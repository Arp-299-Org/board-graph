#!/usr/bin/env python3
"""The pure parts of routing_run.py: the adoption guard, the outcome, the note, the evidence lines and the run's state
on the Mac. No Mac, no git, no KiCad."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import sys
import tempfile
import unittest
import zipfile
from contextlib import ExitStack, redirect_stdout
from io import StringIO
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
# Fixture board description and machine settings, read at import so every checkout derives the same names.
FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
os.environ["BOARD_GRAPH_BOARD"] = str(FIXTURES / "board.json")
os.environ["BOARD_GRAPH_SITE"] = str(FIXTURES / "board_graph_site.json")
# The tests read every checkout as a repository with one board: a project's own list of boards (scripts/boards.py)
# is not read here. The file named is not there.
os.environ["BOARD_GRAPH_BOARDS"] = str(pathlib.Path(__file__).resolve().parent / "fixtures" / "no_boards.json")
import routing_run  # noqa: E402

REAL_GRAPH = routing_run.board_graph.Graph
REAL_RECORD = routing_run.board_graph.record
REAL_TERMS = routing_run.board_graph.check_routing_terms

BOARD = """(kicad_pcb
\t(version 20260206)
\t(generator "pcbnew")
\t(generator_version "10.0")
\t(setup (pad_to_mask_clearance 0))
\t(footprint "R_0402" (layer "F.Cu") (uuid "f1") (at 10 20 90)
\t\t(property "Reference" "R36" (at 0 0 0))
\t\t(pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net "GND"))
\t)
\t(gr_text "DEMO" (at 5 5 0) (layer "F.SilkS") (uuid "t1"))
\t(segment (start 1 1) (end 2 2) (width 0.2) (layer "F.Cu") (net "GND") (uuid "s1"))
\t(via (at 3 3) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "GND") (uuid "v1"))
\t(zone (net "GND") (layers "In1.Cu") (uuid "z1") (name "ground")
\t\t(polygon (pts (xy 0 0) (xy 50 0) (xy 50 50)))
\t\t(filled_polygon (layer "In1.Cu") (pts (xy 0 0) (xy 49 0) (xy 49 49)))
\t)
)
"""

NO_AGENT = "none: a stand-in agent (--agent) ran, with no agent transcript to count"


def step(task, kept, why="editor_work", before=(19, 19, 46), after=(18, 19, 45), other=0, used=None, review_open=()):
    check = lambda n: {"open": n[0], "errors": n[1], "findings": n[2]}  # noqa: E731
    calls = NO_AGENT if used is None else {"route": 2, "route_closed": 1, "route_refused": 1, "route_by_status": {},
                                           "edit": 0, "edit_by_verb": {}, "look": 1, "other": other, "repeats": {}}
    tokens = NO_AGENT if used is None else {"input": used, "output": 10, "thinking": 5, "cache_read": 1000,
                                            "cache_write": 100}
    return {"task": task, "kept": kept, "why": why, "before": check(before), "after": check(after), "rounds": 1,
            "reviews": 1, "review_open": list(review_open), "seconds": 61.5, "calls": calls, "tokens_total": tokens,
            "board": "/Users/x/run/board/demo.kicad_pcb", "tokens": {"agent": {"rounds": []}}, "folder": "/x"}


SUMMARY = {"stopped": "not kept: 2-undo, check_failed", "kept": 1, "tasks": 2, "tokens_total": NO_AGENT,
           "route_calls": NO_AGENT, "route_closed": NO_AGENT, "route_refused": NO_AGENT, "editor_pids": [1]}


class Guard(unittest.TestCase):
    def test_tracks_arcs_and_vias_may_change(self):
        board = BOARD.replace("(end 2 2)", "(end 4 4)").replace("(at 3 3)", "(at 3.5 3)")
        board = board.replace("\t(zone", '\t(arc (start 1 1) (mid 2 3) (end 3 1) (width 0.2) (layer "B.Cu") (net "GND") (uuid "a1"))\n\t(zone')
        self.assertEqual(routing_run.guard(BOARD, board), [])

    def test_a_refill_and_another_generator_do_not_count(self):
        board = BOARD.replace("(xy 49 49)", "(xy 48 47)").replace('(generator_version "10.0")', '(generator_version "10.1")')
        self.assertEqual(routing_run.guard(BOARD, board), [])

    def test_order_does_not_count(self):
        lines = BOARD.splitlines()
        moved = "\n".join([lines[0], *lines[9:11], *lines[1:9], *lines[11:]])
        self.assertEqual(routing_run.guard(BOARD, moved), [])

    def test_a_moved_footprint_is_refused(self):
        self.assertEqual(routing_run.guard(BOARD, BOARD.replace("(at 10 20 90)", "(at 10.5 20 90)")),
                         ["footprint R36 changed"])

    def test_a_changed_pour_is_refused(self):
        self.assertEqual(routing_run.guard(BOARD, BOARD.replace("(xy 50 50)", "(xy 50 60)")),
                         ["pour ground on GND changed"])

    def test_allow_pours_permits_copper_pours_only_and_guards_rule_areas(self):
        base = BOARD.replace(
            "\t(zone",
            '\t(zone (net 0) (layers "F.Cu" "B.Cu") (uuid "ka1") (name "ko")\n'
            '\t\t(keepout (tracks not_allowed) (vias not_allowed) (pads allowed) (copperpour not_allowed))\n'
            '\t\t(polygon (pts (xy 5 5) (xy 8 5) (xy 8 8) (xy 5 8)))\n\t)\n'
            '\t(zone (net 0) (layer "F.Mask") (uuid "m1") (name "maskz")\n'
            '\t\t(polygon (pts (xy 1 1) (xy 2 1) (xy 2 2) (xy 1 2)))\n\t)\n\t(zone',
        )
        added_pour = base.replace(
            "\t(zone",
            '\t(zone (net "GND") (layers "In2.Cu") (uuid "z2") (name "g2")\n'
            '\t\t(polygon (pts (xy 0 0) (xy 40 0) (xy 40 40)))\n\t)\n\t(zone',
            1,
        ).replace("(xy 50 50)", "(xy 50 60)")
        self.assertTrue(routing_run.guard(base, added_pour, allow_pours=False))
        self.assertEqual(routing_run.guard(base, added_pour, allow_pours=True), [])

        # Rule area edits, non-copper mask zone edits, moved footprints and setup edits are STILL refused.
        changed_ko = added_pour.replace("(xy 8 8)", "(xy 9 8)")
        self.assertEqual(routing_run.guard(base, changed_ko, allow_pours=True), ["rule area ko on 0 changed"])
        changed_mask = added_pour.replace("(xy 2 2)", "(xy 3 2)")
        self.assertEqual(routing_run.guard(base, changed_mask, allow_pours=True), ["pour maskz on 0 changed"])
        moved_fp = added_pour.replace("(at 10 20 90)", "(at 11 20 90)")
        self.assertEqual(routing_run.guard(base, moved_fp, allow_pours=True), ["footprint R36 changed"])
        changed_setup = added_pour.replace("(pad_to_mask_clearance 0)", "(pad_to_mask_clearance 0.05)")
        self.assertEqual(routing_run.guard(base, changed_setup, allow_pours=True), ["setup changed"])

    def test_a_changed_text_or_setting_is_refused(self):
        self.assertEqual(routing_run.guard(BOARD, BOARD.replace('"DEMO"', '"DEMO2"')), ["text 'DEMO' removed",
                                                                                      "text 'DEMO2' added"])
        self.assertEqual(routing_run.guard(BOARD, BOARD.replace("(pad_to_mask_clearance 0)", "(pad_to_mask_clearance 0.05)")),
                         ["setup changed"])

    def test_not_a_board_raises(self):
        with self.assertRaises(ValueError):
            routing_run.guard(BOARD, "(kicad_sch)")

    def test_angle_normalization_mod_360_in_guard_and_reload(self):
        import types
        base = (
            '(kicad_pcb\n'
            '\t(footprint "Conn" (layer "F.Cu") (uuid "f1") (at 10 20 0)\n'
            '\t\t(property "Reference" "JB1" (at 0 8.6 450))\n'
            '\t\t(property "Value" "V" (at 0 0 360))\n'
            '\t\t(property "Datasheet" "" (at 0 0 -90))\n'
            '\t\t(property "Description" "" (at 0 0 450 unlocked))\n'
            '\t\t(pad "1" smd rect (at 0 0 720) (size 0.5 0.5) (layers "F.Cu") (net "GND"))\n'
            '\t)\n)'
        )
        normalized = (
            '(kicad_pcb\n'
            '\t(footprint "Conn" (layer "F.Cu") (uuid "f1") (at 10 20)\n'
            '\t\t(property "Reference" "JB1" (at 0 8.6 90))\n'
            '\t\t(property "Value" "V" (at 0 0))\n'
            '\t\t(property "Datasheet" "" (at 0 0 270))\n'
            '\t\t(property "Description" "" (at 0 0 90 unlocked))\n'
            '\t\t(pad "1" smd rect (at 0 0 0) (size 0.5 0.5) (layers "F.Cu") (net "GND"))\n'
            '\t)\n)'
        )
        self.assertEqual(routing_run.guard(base, normalized), [])
        with mock.patch.dict(sys.modules, {"kipy": types.SimpleNamespace(KiCad=object)}):
            import kicad_reload_board
            self.assertEqual(kicad_reload_board.board_items(base), kicad_reload_board.board_items(normalized))

            rotated = normalized.replace("(at 0 8.6 90)", "(at 0 8.6 180)")
            self.assertEqual(routing_run.guard(base, rotated), ["footprint JB1 changed"])
            self.assertNotEqual(kicad_reload_board.board_items(base), kicad_reload_board.board_items(rotated))

            moved = normalized.replace("(at 10 20)", "(at 10.5 20)")
            self.assertEqual(routing_run.guard(base, moved), ["footprint JB1 changed"])
            self.assertNotEqual(kicad_reload_board.board_items(base), kicad_reload_board.board_items(moved))


class ResumedAdoption(unittest.TestCase):
    def setUp(self):
        self.resources = ExitStack()
        self.addCleanup(self.resources.close)
        root = pathlib.Path(self.resources.enter_context(tempfile.TemporaryDirectory()))
        self.root = root.resolve()
        self.board = root / routing_run.BOARD
        self.board.parent.mkdir(parents=True)
        self.board.write_bytes(BOARD.encode())
        self.download = BOARD.replace("(end 2 2)", "(end 4 4)").encode()
        self.digest = hashlib.sha256(self.download).hexdigest()
        self.rel = "board/routing/tasks"
        self.attempt = "1234567890abcdef"
        self.receipt = root / "board/routing/run-12345678.jsonl"
        self.receipt.parent.mkdir(parents=True)
        branch = "codex/route"
        local_git = routing_run.routing_delivery.git
        remote = pathlib.Path(self.resources.enter_context(tempfile.TemporaryDirectory()))
        local_git(remote, "init", "--bare", "-q")
        local_git(root, "init", "-q", "-b", branch)
        for key, value in (("user.name", "Test"), ("user.email", "test@example.test"), ("core.autocrlf", "false")):
            local_git(root, "config", key, value)
        self.tasks = root / self.rel
        self.tasks.mkdir()
        for name in ("first", "second"):
            (self.tasks / name).write_text("Repair: change copper")
        local_git(root, "add", ".")
        local_git(root, "commit", "-q", "-m", "base")
        local_git(root, "remote", "add", "origin", str(remote))
        self.meta = dict(branch=branch, tasks=self.rel, attempt=self.attempt,
                         commit=local_git(root, "rev-parse", "HEAD"), provider="codex")
        self.proof = dict(provider="codex", review_provider="codex", review_model="reviewer",
                          reviewed_sha256=self.digest, review_receipt=dict(schema="board-graph/routing-review/v1",
                          board_sha256=self.digest, reviewer="reviewer", answer="NOTHING.", earlier=[], warnings=[]))
        self.last = dict(step("second", True), **self.proof)
        self.stopped = None
        def fetch(path):
            if path.endswith("/run.json"):
                return json.dumps(self.meta).encode()
            if path.endswith("/out.jsonl"):
                first = dict(step("first", True), **self.proof)
                if self.stopped:
                    return "\n".join(map(json.dumps, (first, dict(SUMMARY, kept=1, stopped=self.stopped)))).encode()
                summary = dict(SUMMARY, kept=1 + int(self.last["kept"]), stopped="end" if self.last["kept"] else SUMMARY["stopped"])
                return "\n".join(map(json.dumps, (first, self.last, summary))).encode()
            self.assertTrue(path.endswith("/" + routing_run.BOARD))
            return self.download
        self.resources.enter_context(mock.patch.object(routing_run, "ROOT", root))
        self.state = mock.Mock()
        self.state.records = root / "records"
        self.state.transitions.return_value = []
        for name, value in dict(preflight=(branch, self.rel), engine=(None, self.state, dict(how="--state", path="db")), start=self.attempt,
                                status={"state": "finished"}, wait={"state": "finished"}).items():
            self.resources.enter_context(mock.patch.object(routing_run, name, return_value=value))
        self.resources.enter_context(mock.patch.object(routing_run, "fetch", side_effect=fetch))
        self.resources.enter_context(mock.patch.object(routing_run.mac, "ssh", return_value=mock.Mock(returncode=0, stdout=b"abc")))
        self.record = mock.Mock()
        self.resources.enter_context(mock.patch.multiple(routing_run.board_graph, Graph=mock.Mock(),
                                    check_routing_terms=mock.Mock(), record=self.record,
                                    carry=mock.Mock(return_value=""), recorded=mock.Mock(return_value="")))
        self.args = argparse.Namespace(tasks=self.tasks, attempt=self.attempt, provider="codex", by="agent:test",
                                       note="", agent=None, graph=root / "graph.json", mac_tree="~/tree", mac_steps="~/steps")

    def resume(self, status):
        with redirect_stdout(StringIO()):
            self.assertEqual(routing_run.run(self.args), status)
        return json.loads(self.receipt.read_text().splitlines()[-1])

    def test_final_kept_step_needs_its_own_matching_review_hash(self):
        for field in ({}, {"reviewed_sha256": None}, {"reviewed_sha256": ""},
                      {"reviewed_sha256": "malformed"}, {"reviewed_sha256": "0" * 64}):
            with self.subTest(field=field):
                self.last = dict(step("second", True), **field)
                result = self.resume(2)
                self.assertFalse(result["adopted"])
                self.assertEqual(result["outcome"], "needs_lead")
                self.assertEqual(self.board.read_bytes(), BOARD.encode())
                self.assertEqual(self.record.call_args.args[3], "needs_lead")
                self.assertEqual(routing_run.routing_delivery.git(self.root, "show", "HEAD:" + routing_run.BOARD), BOARD.strip())
                candidate = result["unadopted_candidate"]
                self.assertEqual(candidate["board_sha256"], self.digest)
                self.assertIn(candidate["evidence"], self.record.call_args.args[6])
                self.assertTrue(routing_run.routing_delivery.git(self.root, "ls-files", "--", candidate["evidence"]))
                with zipfile.ZipFile(routing_run.ROOT / candidate["evidence"]) as archive:
                    self.assertEqual(archive.read("candidate.kicad_pcb"), self.download)
        self.assertEqual(len(list(self.receipt.parent.glob("*.zip"))), 1)

    def test_refusal_keeps_copper_private_and_retry_never_contacts_the_operator(self):
        self.record.side_effect = ValueError("superseded attempt")
        with self.assertRaisesRegex(routing_run.Refused, "superseded attempt"), redirect_stdout(StringIO()):
            routing_run.run(self.args)
        self.assertEqual(self.board.read_bytes(), BOARD.encode())
        pending = json.loads(routing_run.routing_delivery.journal(self.root).read_bytes())
        self.assertEqual((pathlib.Path(pending["stage"]) / routing_run.BOARD).read_bytes(), self.download)
        self.record.side_effect = None
        with mock.patch.object(routing_run, "fetch", side_effect=AssertionError("re-launched")):
            self.resume(0)
        self.assertEqual(self.board.read_bytes(), self.download)

    def test_mac_receipt_through_real_graph_and_publication_matches_the_remote_bytes(self):
        bg = routing_run.board_graph
        data = json.loads((FIXTURES / "board_graph.json").read_bytes())
        card = dict(next(n for n in data["nodes"] if n["id"] == routing_run.NODE))
        card.update(action="routing_run", docs=[], owns=[routing_run.BOARD], requires=[], outcomes={"pass":"done","returned":[]})
        card.pop("unchanged_pass", None)
        data.update(approval_mode="enforce", docs=[], open_issues=[], nodes=[card],
                    tracks=[dict(id="board", label="Board", ordered=True, start=routing_run.NODE)])
        self.args.graph.write_text(json.dumps(data))
        (self.root / "AGENTS.md").write_text("Routing test instructions")
        (self.root / routing_run.DESCRIPTION.rel("kicad_project")).write_text("{}")
        local_git = routing_run.routing_delivery.git
        local_git(self.root, "add", ".")
        local_git(self.root, "commit", "-q", "-m", "Project contract")
        local_git(self.root, "update-ref", "refs/remotes/origin/main", "HEAD")
        graph = REAL_GRAPH(self.args.graph)
        outside = pathlib.Path(self.resources.enter_context(tempfile.TemporaryDirectory()))
        state = bg.State(outside / "db", self.root / "records")
        terms = bg.approval_inputs(graph, state, routing_run.NODE, self.root)
        state.open_work(graph, routing_run.NODE, self.args.by, "test", terms, str(self.root), run_id=self.attempt)
        with mock.patch.object(routing_run, "engine", return_value=(graph,state,dict(how="ssh",path="fixture"))), \
             mock.patch.multiple(bg, Graph=REAL_GRAPH, record=REAL_RECORD, check_routing_terms=REAL_TERMS), \
             mock.patch.object(bg, "keep_pull_request", return_value=""):
            self.resume(0)
        row, = state.transitions()
        self.assertEqual(row["outcome"], "more_work")
        self.assertEqual(row["inputs"]["attempt_id"], self.attempt)
        remote = pathlib.Path(local_git(self.root, "remote", "get-url", "origin"))
        self.assertEqual(local_git(remote, "rev-parse", "refs/heads/codex/route"), local_git(self.root,"rev-parse","HEAD"))
        self.assertEqual(json.loads(local_git(remote, "show", "codex/route:records/" + row["record_name"])),
                         json.loads(bg.record_text(row)))
        self.assertEqual(row["inputs"][routing_run.BOARD], hashlib.sha256(self.board.read_bytes()).hexdigest())
        self.assertFalse(routing_run.routing_delivery.journal(self.root).exists())

    def test_push_retry_finds_prior_acceptance_and_does_not_record_or_route_again(self):
        real_git = routing_run.routing_delivery.git
        def fail_push(root, verb, *args):
            if verb == "push":
                raise routing_run.routing_delivery.DeliveryError("transport stopped")
            return real_git(root, verb, *args)
        with mock.patch.object(routing_run.routing_delivery, "git", side_effect=fail_push), redirect_stdout(StringIO()):
            with self.assertRaisesRegex(routing_run.Refused, "transport stopped"):
                routing_run.run(self.args)
        data = json.loads(routing_run.routing_delivery.journal(self.root).read_bytes())
        self.state.transitions.return_value = [dict(inputs={"attempt_id": self.attempt}, outcome=data["outcome"],
                actor=self.args.by, evidence=[dict(path=data["files"][0], sha256=data["receipt_sha256"])])]
        self.assertEqual(self.board.read_bytes(), self.download)
        with mock.patch.object(routing_run, "fetch", side_effect=AssertionError("contacted operator")):
            self.resume(0)
        self.assertEqual(self.record.call_count, 1)
        self.assertFalse(routing_run.routing_delivery.journal(self.root).exists())

    def test_deadline_between_tasks_delivers_reviewed_prefix_and_names_unfinished_work(self):
        self.stopped = "Routing card deadline reached"
        receipt = self.resume(2)
        self.assertTrue(receipt["adopted"])
        self.assertEqual(receipt["outcome"], "needs_lead")
        self.assertEqual(receipt["unfinished_tasks"], ["second"])
        self.assertIn("second", self.record.call_args.args[5])
        self.assertEqual(self.board.read_bytes(), self.download)

    def test_complete_summary_cannot_cover_an_unreported_assignment(self):
        self.stopped = "every task was kept"
        receipt = self.resume(2)
        self.assertFalse(receipt["adopted"])
        self.assertEqual(self.board.read_bytes(), BOARD.encode())

    def test_partial_publication_survives_retain_only_and_resumes_once(self):
        self.last = step("second", False)
        real_git = routing_run.routing_delivery.git
        def fail_push(root, verb, *args):
            if verb == "push":
                raise routing_run.routing_delivery.DeliveryError("transport stopped")
            return real_git(root, verb, *args)
        with mock.patch.object(routing_run.routing_delivery, "git", side_effect=fail_push), redirect_stdout(StringIO()):
            with self.assertRaisesRegex(routing_run.Refused, "transport stopped"):
                routing_run.run(self.args)
        marker = routing_run.routing_delivery.journal(self.root)
        data = json.loads(marker.read_bytes())
        self.assertEqual(data["outcome"], "needs_lead")
        self.assertEqual(data["pcb"], routing_run.BOARD)
        self.state.transitions.return_value = [dict(inputs={"attempt_id": self.attempt}, outcome=data["outcome"],
                actor=self.args.by, evidence=[dict(path=data["files"][0], sha256=data["receipt_sha256"])])]
        self.state.attempts.return_value = []
        self.args.retain_only = True
        with self.assertRaisesRegex(routing_run.Refused, "Resume publication"):
            routing_run.run(self.args)
        self.assertTrue(marker.is_file())
        self.args.retain_only = False
        with mock.patch.object(routing_run, "fetch", side_effect=AssertionError("called the operator")):
            self.resume(2)
        remote = pathlib.Path(real_git(self.root, "remote", "get-url", "origin"))
        self.assertEqual(real_git(remote, "rev-parse", "refs/heads/codex/route"), data["commit"])
        self.assertEqual(self.board.read_bytes(), self.download)
        self.assertFalse(marker.exists())
        self.assertEqual(self.record.call_count, 1)

    def test_matching_review_hash_adopts_download_and_records_more_work(self):
        self.last["reviewed_sha256"] = self.digest
        result = self.resume(0)
        self.assertTrue(result["adopted"])
        self.assertEqual(result["outcome"], "more_work")
        self.assertEqual(self.board.read_bytes(), self.download)
        self.assertNotIn("unadopted_candidate", result)
        self.assertEqual(self.record.call_args.args[3], "more_work")

    def test_resumed_cross_provider_rejects_mismatch_and_records_review_provider(self):
        self.meta["review_provider"] = "gemini"
        self.proof["review_provider"] = "gemini"
        self.last = dict(step("second", True), **self.proof)
        self.args.review_provider = "codex"
        with self.assertRaisesRegex(routing_run.Refused, "already uses review provider gemini"), redirect_stdout(StringIO()):
            routing_run.run(self.args)
        self.args.review_provider = "gemini"
        receipt = self.resume(0)
        self.assertTrue(receipt["adopted"])
        self.assertEqual((receipt["provider"], receipt["review_provider"]), ("codex", "gemini"))


class Outcome(unittest.TestCase):
    def test_a_partial_batch_stops_for_the_lead(self):
        self.assertEqual(routing_run.outcome([step("1", True), step("2", False)], SUMMARY, []), "needs_lead")
        self.assertEqual(routing_run.outcome([step("1", True)], dict(SUMMARY, tasks=1), []), "more_work")

    def test_none_kept_a_change_beyond_copper_or_no_summary_is_needs_lead(self):
        self.assertEqual(routing_run.outcome([step("1", False)], SUMMARY, []), "needs_lead")
        self.assertEqual(routing_run.outcome([step("1", True)], SUMMARY, ["footprint R36 changed"]), "needs_lead")
        self.assertEqual(routing_run.outcome([step("1", True)], None, []), "needs_lead")
        self.assertEqual(routing_run.outcome([], SUMMARY, []), "needs_lead")

    def test_never_pass(self):
        for steps in ([step("1", True)] * 3, [step("1", False)], []):
            self.assertIn(routing_run.outcome(steps, SUMMARY, []), ("more_work", "needs_lead"))

    def test_tokens(self):
        self.assertIsNone(routing_run.tokens([step("1", True)]))
        self.assertIsNone(routing_run.tokens([]))
        # input, output and cache; thinking is inside output and not added again
        self.assertEqual(routing_run.tokens([step("1", True, used=200), step("2", False, used=300)]),
                         2 * (10 + 1000 + 100) + 500)


class Note(unittest.TestCase):
    def test_a_stand_in_run(self):
        steps = [step("1-route", True), step("2-undo", False, why="check_failed", after=(19, 19, 46))]
        text = routing_run.note(steps, SUMMARY, [], "0123456789abcdef")
        self.assertEqual(text, "Routing run 01234567 in the Mac's PCB Editor: 1 of 2 steps kept. Open connections 19 "
                               "to 18, errors 19 to 19, findings 46 to 45. Route calls: not counted (a stand-in agent). "
                               "Tokens: none counted (a stand-in agent). Not kept: 2-undo (check_failed). Stopped: not "
                               "kept: 2-undo, check_failed. Adopted: the board after the last kept step, tracks, arcs "
                               "and vias changed only.")
        self.assertNotIn("$", text)
        self.assertNotIn("—", text)

    def test_counts_other_commands_and_open_reviews(self):
        summary = dict(SUMMARY, route_calls=4, route_closed=2, route_refused=2)
        steps = [step("a", True, used=100, other=3), step("b", False, used=100, review_open=["MUST FIX: x"])]
        text = routing_run.note(steps, summary, [], "ffffffff00")
        self.assertIn("Route calls 4: 2 closed, 2 refused.", text)
        self.assertIn("Tokens: 2,420.", text)
        self.assertIn("Not kept: b (editor_work, the review left 1 MUST FIX open).", text)
        self.assertIn("read their transcripts: a (3).", text)

    def test_not_adopted_and_no_summary(self):
        text = routing_run.note([step("a", True)], SUMMARY, ["footprint R36 changed"], "a" * 32)
        self.assertIn("Not adopted: the board changed beyond tracks, arcs and vias: footprint R36 changed.", text)
        self.assertNotIn("Adopted:", text)
        text = routing_run.note([], None, [], "a" * 32, "The runner ended without a summary (died): boom.")
        self.assertIn("0 of 0 steps kept. The runner ended without a summary (died): boom.", text)
        self.assertIn("Open connections unknown", text)


class Evidence(unittest.TestCase):
    def test_candidate_is_hash_checked_before_it_reaches_the_desktop(self):
        data = b"candidate archive"
        candidate = {"step_folder": "step-1118-083117", "sha256": hashlib.sha256(data).hexdigest()}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(routing_run, "ROOT", pathlib.Path(tmp)):
            receipt = pathlib.Path(tmp) / "run-2e8ae57d.jsonl"
            with mock.patch.object(routing_run, "fetch", return_value=b"wrong"):
                with self.assertRaisesRegex(routing_run.Refused, "differs"):
                    routing_run.collect_candidates([{"candidate": candidate}], "/mac/run", receipt)
                self.assertEqual(list(receipt.parent.iterdir()), [])
            with mock.patch.object(routing_run, "fetch", return_value=data):
                names = routing_run.collect_candidates([{"candidate": candidate}], "/mac/run", receipt)
            self.assertEqual(names, [candidate["evidence"]])
            self.assertEqual((receipt.parent / names[0]).read_bytes(), data)

    def test_reviewed_candidates_survive_later_rounds_and_a_final_failed_check(self):
        payloads = {"candidate-round1.zip": b"first board", "candidate-round2.zip": b"corrected board"}
        reviews = [{"candidate": {"step_folder": "step-1118-152003", "archive": name,
                                  "sha256": hashlib.sha256(data).hexdigest()}} for name, data in payloads.items()]
        def fetch(path):
            return payloads[pathlib.PurePosixPath(path).name]
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(routing_run, "ROOT", pathlib.Path(tmp)):
            receipt = pathlib.Path(tmp) / "run-17b301e5.jsonl"
            for final in ({}, {"candidate": dict(reviews[-1]["candidate"])}):
                with mock.patch.object(routing_run, "fetch", side_effect=fetch) as download:
                    names = routing_run.collect_candidates([dict(final, warning_reviews=reviews)], "/mac/run", receipt)
                self.assertEqual(download.call_count, 2)
                self.assertEqual(set(names), {entry["candidate"]["evidence"] for entry in reviews})
                for entry in reviews:
                    candidate = entry["candidate"]
                    self.assertEqual((receipt.parent / candidate["evidence"]).read_bytes(), payloads[candidate["archive"]])
                if final:
                    self.assertEqual(final["candidate"]["evidence"], reviews[-1]["candidate"]["evidence"])

    def test_one_line_per_step_then_the_summary(self):
        steps = [step("1-route", True), step("2-undo", False)]
        lines = routing_run.evidence_lines(steps, SUMMARY, {"attempt": "abc", "adopted": True})
        self.assertEqual(len(lines), 3)
        first = json.loads(lines[0])
        self.assertEqual(list(first), list(routing_run.STEP_FIELDS))
        self.assertNotIn("tokens", first)  # per round and model: in the runner's folder, not here
        last = json.loads(lines[-1])
        self.assertEqual((last["summary"], last["kept"], last["tasks"], last["attempt"], last["adopted"]),
                         (True, 1, 2, "abc", True))
        self.assertNotIn("editor_pids", last)
        with self.assertRaises(ValueError):  # the run's own facts never hide the runner's count
            routing_run.evidence_lines(steps, SUMMARY, {"tasks": "board/reports/x/tasks"})

    def test_results_from_the_runners_output(self):
        out = "\n".join(["noise", json.dumps(step("1", True)), "{bad", json.dumps(SUMMARY)])
        steps, summary = routing_run.results(out)
        self.assertEqual(([s["task"] for s in steps], summary["kept"]), (["1"], 1))
        self.assertEqual(routing_run.results(json.dumps(step("1", True))), ([step("1", True)], None))


class RunState(unittest.TestCase):
    def test_states(self):
        say = "phase={}\nexit={}\nalive={}\nholder={}\nsteps={}\n".format
        self.assertEqual(routing_run.run_state("absent\n"), {"state": "absent"})
        self.assertEqual(routing_run.run_state(say("waiting", "", 1, "selftest ", 0))["state"], "waiting")
        self.assertEqual(routing_run.run_state(say("waiting", "", 1, "selftest ", 0))["holder"], "selftest")
        going = routing_run.run_state(say("running", "", 1, "routing-run-x ", 1))
        self.assertEqual((going["state"], going["steps"]), ("running", 1))
        with_detail = routing_run.run_state(say("running", "", 1, "", 0) + "detail=round 1 operator: kicad_lay.py\n")
        self.assertEqual(with_detail["detail"], "round 1 operator: kicad_lay.py")
        done = routing_run.run_state(say("done", 1, 0, "", 2))
        self.assertEqual((done["state"], done["exit"], done["steps"]), ("finished", 1, 2))
        self.assertEqual(routing_run.run_state(say("running", "", 0, "", 1))["state"], "died")
        self.assertEqual(routing_run.run_state(say("", "", 0, "", ""))["state"], "died")  # launched, never wrote

    def test_one_machine_routes_from_the_existing_checkout(self):
        branch = "build/route"
        with mock.patch.object(routing_run.mac, "ONE_MACHINE", True):
            self.assertEqual(routing_run.routing_tree(None, branch), str(routing_run.ROOT.resolve()))
            self.assertEqual(routing_run.routing_tree("/explicit", branch), "/explicit")
        with mock.patch.object(routing_run.mac, "ONE_MACHINE", False):
            self.assertEqual(routing_run.routing_tree(None, branch), routing_run.mac.SITE.worktree(branch))

    def test_the_wrapper_takes_and_gives_back_the_editor(self):
        args = argparse.Namespace(mac_steps="~/demo-steps/routing-run", minutes=3, model=None, effort="low", provider=None,
                                  review_model=None, agent='eval "$TASK"')
        text = routing_run.wrapper(args, "abc", "~/demo-x", "board/reports/r/tasks", "c0ffee")
        self.assertIn('run="$HOME/demo-steps/routing-run/run-abc"', text)
        self.assertIn('until mkdir "$lock" 2>/dev/null; do', text)
        self.assertIn("trap 'rm -f \"$holder\"; rmdir \"$lock\" 2>/dev/null' EXIT", text)
        self.assertLess(text.index("mkdir \"$lock\""), text.index("trap 'rm"))  # never removes a lock it did not make
        self.assertIn('"$HOME/demo-steps/routing-run/run-abc/board/demo.kicad_pcb" --tasks '
                      '"$HOME/demo-x/board/reports/r/tasks" --minutes 3 --effort low --agent \'eval "$TASK"\'', text)
        self.assertIn('reclaimed stale lock', text)
        self.assertIn('BOARD_GRAPH_STATUS_FILE="$run/status"', text)
        self.assertIn('echo $? > "$run/exit"', text)

    def test_later_edits_must_preserve_an_earlier_connection_on_the_final_revision(self):
        import editor_step
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            pcb = root / "board.pcb"
            pcb.write_text("saved geometry")
            pair = {"from":"SP1.1","to":"J1.1","net":"A"}
            steps = [{"kept":True,"task":"first","connection":pair},{"kept":True,"task":"last"}]
            with mock.patch.object(editor_step, "saved_connection", return_value=False):
                with self.assertRaisesRegex(ValueError, "connection reopened"):
                    editor_step.final_connections(pcb, steps, root)
            with mock.patch.object(editor_step, "saved_connection", return_value=True):
                proof = editor_step.final_connections(pcb, steps, root)
            self.assertEqual(proof, {"board_sha256":hashlib.sha256(pcb.read_bytes()).hexdigest(),
                                     "connections":{"first":["SP1.1","J1.1","A"]}})

    def test_card_budget_caps_model_calls_and_time_waiting_for_the_lock(self):
        import editor_step
        args = argparse.Namespace(mac_steps="~/steps", minutes=3, batch_minutes=5, model=None, effort=None,
                                  provider=None, review_model=None, agent=None)
        wrapper = routing_run.wrapper(args, "abc", "~/tree", "tasks", "abc123")
        self.assertIn("BOARD_GRAPH_ROUTE_DEADLINE=$(( $(date +%s) + 300 ))", wrapper)
        self.assertLess(wrapper.index("Card deadline expired"), wrapper.index('> "$holder"'))
        with mock.patch.dict(os.environ, BOARD_GRAPH_ROUTE_DEADLINE="600"):
            self.assertEqual(editor_step.batch_seconds(180, now=570), 30)
            self.assertEqual(editor_step.batch_seconds(180, now=610), 0)

    def test_the_launch_makes_the_kicad_python_environment_before_anything_runs_and_refuses_when_it_cannot(self):
        import argparse
        args = argparse.Namespace(mac_steps="~/steps", minutes=3, model=None, effort=None, review_model=None, agent=None, provider=None)
        with mock.patch.object(routing_run, "mac_bash") as sent, mock.patch.object(routing_run, "git", return_value="cafe"), mock.patch.object(routing_run.board_graph_tool, "differences", return_value=[]):
            receipt = routing_run.prepare_mac("claude/x", "c0ffee", "~/tree")
        self.assertEqual(receipt["manifest_blob"], "cafe")
        text = sent.call_args.args[0]
        self.assertIn('ipc_py="${KICAD_IPC_PYTHON:-$main/.venv-kicad-ipc/bin/python}"', text)
        self.assertIn('if [ ! -x "$ipc_py" ]; then', text)
        self.assertIn("pwsh -NoProfile -File scripts/setup_kicad_ipc.ps1", text)
        self.assertIn("so the step runner cannot start: nothing was run", text)
        self.assertLess(text.index("scripts/board_graph_tool.py check"), text.index("setup_kicad_ipc.ps1"))

    def test_any_valid_model_name_passes_run_args_and_adoption(self):
        import editor_step
        self.assertEqual(editor_step.ROUTING_MODEL, "sonnet")
        for name in ("claude-opus-5-5", "opus", "claude-sonnet-5-5", "sonnet"):
            args = argparse.Namespace(model=name, review_model=name, effort=None, provider="claude", review_provider="claude", agent=None)
            with mock.patch.object(routing_run.boards, "unnamed", return_value=[]):
                profile, adopt_from = routing_run._validate_run_args(args)
            self.assertIsNone(adopt_from)
            self.assertIsInstance(profile, dict)

    def test_shell_words(self):
        self.assertEqual(routing_run.sh("~/a b"), '"$HOME/a b"')
        self.assertEqual(routing_run.sh("it's"), "'it'\"'\"'s'")
        self.assertEqual(routing_run.sh("~/$x"), "'~/$x'")

    def test_codex_selection_reaches_the_locked_editor_and_is_saved_at_launch(self):
        args = argparse.Namespace(mac_steps="~/steps", minutes=2, provider="codex", model="gpt-6.1-terra",
                                  effort="low", review_model="gpt-6.1-terra", agent=None)
        with mock.patch.object(routing_run, "mac_bash") as sent:
            routing_run.launch(args, "abc", "codex/routing", "abc123", "~/tree", "board/routing/tasks", {"runner_commit": "abc123"})
        script = sent.call_args.args[0]
        self.assertIn('"provider": "codex"', script)
        self.assertIn("--provider codex --model gpt-6.1-terra --effort low --review-model gpt-6.1-terra", script)
        self.assertIn('cd "$HOME/tree"', script)
        self.assertIn('"runner_commit": "abc123"', script)
        self.assertNotIn("--agent ", script)
        args.agent = "true"
        with self.assertRaisesRegex(routing_run.Refused, "cannot be combined with --agent"):
            routing_run.run(args)

    def test_codex_graph_evidence_describes_actual_gestures_and_failure(self):
        row = dict(step("motor", False, used=5), provider="codex", review_provider="codex",
                   detail="operator timed out", calls={"editor_calls": {"look": 2, "key": 1}})
        result = json.loads(routing_run.evidence_lines([row], SUMMARY, {})[0])
        self.assertEqual((result["provider"], result["review_provider"], result["detail"]),
                         ("codex", "codex", "operator timed out"))
        record = routing_run.note([row], SUMMARY, [], "abc", provider="codex")
        self.assertIn("Codex editor calls: key 1, look 2.", record)
        self.assertIn("operator timed out", record)
        self.assertNotIn("stand-in", record)

    def test_codex_shell_run_reports_route_calls(self):
        row = dict(step("motor", True, used=5), provider="codex", review_provider="codex",
                   calls={"route": 1, "route_closed": 1, "route_refused": 0, "edit": 0, "look": 1, "mac": 0, "other": 0})
        summary = dict(SUMMARY, route_calls=1, route_closed=1, route_refused=0)
        record = routing_run.note([row], summary, [], "abc", provider="codex")
        self.assertIn("Route calls 1: 1 closed, 0 refused.", record)
        self.assertNotIn("Codex editor calls", record)

    def test_preflight_flag_and_automatic_task_check_before_mac(self):
        pcb = """(kicad_pcb
            (footprint "Conn" (layer "F.Cu") (at 0 0 0)
                (property "Reference" "J1" (at 0 0 0))
                (pad "1" smd roundrect (at 0 0 0) (size 0.5 1.5) (roundrect_rratio 0.25) (layers "F.Cu") (net 1 "/S1"))
                (pad "2" smd circle (at 2 0 0) (size 0.5 0.5) (layers "F.Cu") (net 2 "/S2"))
            )
            (footprint "Conn" (layer "F.Cu") (at 0 5 0)
                (property "Reference" "J2" (at 0 0 0))
                (pad "1" smd rect (at 0 0 0) (size 0.5 1.5) (layers "F.Cu") (net 1 "/S1"))
                (pad "2" smd rect (at 2 0 0) (size 0.5 1.5) (layers "F.Cu") (net 2 "/S2"))
            )
        )"""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            board = root / routing_run.BOARD
            board.parent.mkdir(parents=True)
            board.write_text(pcb, encoding="utf-8")
            tasks = root / "tasks"
            tasks.mkdir()
            (tasks / "01-j1.txt").write_text("Connection: J1.1 J2.1 /S1\nPad exits: centered\n", encoding="utf-8")
            with mock.patch.object(routing_run, "ROOT", root):
                out = StringIO()
                with redirect_stdout(out):
                    self.assertEqual(routing_run.main([str(tasks), "--preflight"]), 0)
                self.assertIn("preflight ok: 1 task(s)", out.getvalue())
                (tasks / "02-bad.txt").write_text("Connection: J1.2 J2.2 /S2\nPad exits: centered\n", encoding="utf-8")
                err = StringIO()
                with mock.patch.object(sys, "stderr", err):
                    self.assertEqual(routing_run.main([str(tasks), "--preflight"]), 1)
                self.assertIn("02-bad.txt: J1.2:", err.getvalue())
                # Automatic preflight in run() stops before level_with_main / prepare_mac / start.
                fake_state = mock.Mock()
                fake_state.records = root / "records"
                fake_state.attempts.return_value = []
                args = argparse.Namespace(
                    tasks=tasks, by="agent:codex", preflight=False, attempt=None, retain_only=False,
                    note="", minutes=6, model="gpt-6.1-terra", provider="codex", effort="low",
                    review_model="gpt-6.1-terra", agent=None, mac_tree=None, mac_steps="~/steps",
                    state=None, graph=root / "graph.json", records=None,
                )
                with (mock.patch.object(routing_run, "engine", return_value=(None, fake_state, {"path": "db"})),
                      mock.patch.object(routing_run.routing_delivery, "journal", return_value=root / "no-journal.json"),
                      mock.patch.object(routing_run, "preflight", return_value=("codex/joiner", "tasks")),
                      mock.patch.object(routing_run, "level_with_main") as merged,
                      mock.patch.object(routing_run, "prepare_mac") as mac_prep,
                      mock.patch.object(routing_run, "start") as started):
                    with self.assertRaisesRegex(routing_run.Refused, "task preflight failed"):
                        routing_run.run(args)
                merged.assert_not_called()
                mac_prep.assert_not_called()
                started.assert_not_called()

    def test_cross_provider_adoption_launch_and_resume_guard(self):
        import routing_evidence
        proof = dict(schema="board-graph/routing-review/v1", board_sha256="a" * 64, reviewer="gemini-4-neon-high",
                     answer="NOTHING.", earlier=[], warnings=[])
        kept_step = dict(step("01.txt", True), provider="codex", review_provider="gemini",
                         model="gpt-6.1-terra", review_model="gemini-4-neon-high",
                         reviewed_sha256="a" * 64, review_open=[], reviews=1, pad_exits=[], review_receipt=proof)
        summary = dict(SUMMARY, provider="codex", review_provider="gemini", kept=1, tasks=1, stopped="end")
        self.assertEqual(routing_evidence.adoption_problem([kept_step], summary, "a" * 64, "codex"), "")
        self.assertEqual(routing_evidence.adoption_problem([kept_step], summary, "a" * 64, "codex", review_provider="gemini"), "")
        self.assertNotEqual(routing_evidence.adoption_problem([kept_step], summary, "a" * 64, "codex", review_provider="codex"), "")
        # Legacy v0.18.x receipts omit review_provider; same-provider receipts pass, cross-provider missing it fails.
        legacy_step = {k: v for k, v in kept_step.items() if k != "review_provider"}
        legacy_summary = {k: v for k, v in summary.items() if k != "review_provider"}
        self.assertEqual(routing_evidence.adoption_problem([legacy_step], legacy_summary, "a" * 64, "codex"), "")
        self.assertNotEqual(routing_evidence.adoption_problem([legacy_step], summary, "a" * 64, "codex"), "")

        args = argparse.Namespace(mac_steps="~/steps", minutes=2, provider="codex", review_provider="gemini",
                                  model="gpt-6.1-terra", effort="low", review_model="gemini-4-neon-high", agent=None)
        with mock.patch.object(routing_run, "mac_bash") as sent:
            routing_run.launch(args, "abc", "codex/routing", "abc123", "~/tree", "board/routing/tasks", {"runner_commit": "abc123"})
        script = sent.call_args.args[0]
        self.assertIn('"review_provider": "gemini"', script)
        self.assertIn("--review-provider gemini", script)

    def test_multi_connection_chunk_final_connections_adoption_and_note(self):
        import editor_step
        import routing_evidence
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            pcb = root / "board.pcb"
            pcb.write_text("saved geometry")
            board_sha = hashlib.sha256(pcb.read_bytes()).hexdigest()
            chunk_conns = [
                {"from": "J1.1", "to": "J2.1", "net": "/M1_A", "before": False, "after": True, "saved_sha256": "a" * 64},
                {"from": "J1.1", "to": "J2.2", "net": "/M1_A", "before": False, "after": True, "saved_sha256": "a" * 64},
            ]
            single_conn = {"from": "J1.3", "to": "J2.3", "net": "/M1_B", "before": False, "after": True, "saved_sha256": board_sha}
            prior_recheck = [{"kept": True, "task": "01-chunk.txt", "connections": chunk_conns}, {"kept": True, "task": "02-last.txt"}]
            with mock.patch.object(editor_step, "saved_connection", side_effect=[True, False]):
                with self.assertRaisesRegex(ValueError, "connection reopened"):
                    editor_step.final_connections(pcb, prior_recheck, root)
            with mock.patch.object(editor_step, "saved_connection", return_value=True):
                final = editor_step.final_connections(pcb, prior_recheck, root)
            self.assertEqual(final, {
                "board_sha256": board_sha,
                "connections": {"01-chunk.txt": [["J1.1", "J2.1", "/M1_A"], ["J1.1", "J2.2", "/M1_A"]]},
            })

            proof1 = dict(schema="board-graph/routing-review/v1", board_sha256="a" * 64, reviewer="gemini-4-neon-high",
                          answer="PAD J1.1: straight.\nPAD J2.1: straight.\nPAD J2.2: straight.\nNOTHING.",
                          warnings=[], earlier=[])
            proof2 = dict(schema="board-graph/routing-review/v1", board_sha256=board_sha, reviewer="gemini-4-neon-high",
                          answer="NOTHING.", warnings=[], earlier=[])
            step1 = dict(step("01-chunk.txt", True), provider="codex", review_provider="gemini",
                         model="gpt-6.1-terra", review_model="gemini-4-neon-high", reviewed_sha256="a" * 64,
                         review_open=[], reviews=1, connection=None, connections=chunk_conns,
                         pad_exits=[{"pad": p, "issues": []} for p in ("J1.1", "J2.1", "J2.2")], review_receipt=proof1)
            step2 = dict(step("02-last.txt", True), provider="codex", review_provider="gemini",
                         model="gpt-6.1-terra", review_model="gemini-4-neon-high", reviewed_sha256=board_sha,
                         review_open=[], reviews=1, connection=single_conn, connections=None,
                         pad_exits=[], review_receipt=proof2)
            summary = dict(SUMMARY, provider="codex", review_provider="gemini", kept=2, tasks=2,
                           stopped="every task was kept", final_connections=final)
            expected_tasks = {
                "01-chunk.txt": "Connection: J1.1 J2.1 /M1_A\nConnection: J1.1 J2.2 /M1_A\nPad exits: centered\n",
                "02-last.txt": "Connection: J1.3 J2.3 /M1_B\n",
            }
            self.assertEqual(routing_evidence.adoption_problem([step1, step2], summary, board_sha, "codex",
                                                               expected_tasks, review_provider="gemini"), "")
            # Reject receipt containing both connection and connections, wrong connection, or duplicate/missing pad.
            self.assertNotEqual(routing_evidence.adoption_problem(
                [dict(step1, connection=single_conn), step2], summary, board_sha, "codex", expected_tasks, review_provider="gemini"), "")
            bad_conns = [chunk_conns[0], dict(chunk_conns[1], to="J9.9")]
            self.assertNotEqual(routing_evidence.adoption_problem(
                [dict(step1, connections=bad_conns), step2], summary, board_sha, "codex", expected_tasks, review_provider="gemini"), "")
            self.assertNotEqual(routing_evidence.adoption_problem(
                [dict(step1, pad_exits=step1["pad_exits"][:2]), step2], summary, board_sha, "codex", expected_tasks, review_provider="gemini"), "")

            unadopted = dict(step("01-chunk.txt", False, why="audit_failed"), connections=chunk_conns,
                             candidate={"evidence": "cand.zip"})
            note_text = routing_run.note([unadopted], dict(SUMMARY, kept=0, tasks=1), [], "abc")
            self.assertIn("Saved candidate connects J1.1 to J2.1 on /M1_A, J1.1 to J2.2 on /M1_A.", note_text)

            # Mode validation in adoption_problem and lay_calls formatting in note().
            ipc_tasks = {"01-chunk.txt": "Mode: ipc\n" + expected_tasks["01-chunk.txt"],
                         "02-last.txt": expected_tasks["02-last.txt"]}
            self.assertEqual(routing_evidence.adoption_problem(
                [dict(step1, routing_mode="ipc"), dict(step2, routing_mode="hybrid")],
                summary, board_sha, "codex", ipc_tasks, review_provider="gemini"), "")
            self.assertNotEqual(routing_evidence.adoption_problem(
                [dict(step1, routing_mode="hybrid"), dict(step2, routing_mode="hybrid")],
                summary, board_sha, "codex", ipc_tasks, review_provider="gemini"), "")
            self.assertNotEqual(routing_evidence.adoption_problem(
                [dict(step1, routing_mode="magic"), step2],
                summary, board_sha, "codex", expected_tasks, review_provider="gemini"), "")
            ipc_note = routing_run.note([dict(step1, routing_mode="ipc")],
                                        dict(SUMMARY, route_calls=0, route_closed=0, route_refused=0,
                                             lay_calls=2, lay_laid=2, lay_refused=0),
                                        [], "abc")
            self.assertIn("IPC lay calls 2: 2 laid, 0 refused.", ipc_note)

    def test_legacy_autoroute_mode_adoption_and_note(self):
        import routing_evidence
        sha1 = "a" * 64
        ar_step = dict(
            step("01-ar.txt", True),
            task="01-ar.txt",
            kept=True,
            why="editor_work",
            reviews=1,
            review_open=[],
            provider="gemini",
            review_provider="gemini",
            model="gemini-4-neon",
            review_model="gemini-4-neon",
            reviewed_sha256=sha1,
            routing_mode="autoroute",
            autoroute={"solver": "kicad-routing-tools@v0.24.1", "clean": True},
            connections=[{"from": "J1.1", "to": "J2.1", "net": "/S1", "before": False, "after": True, "saved_sha256": sha1}],
            pad_exits=[{"pad": "J1.1", "issues": []}, {"pad": "J2.1", "issues": []}],
            review_receipt={
                "schema": "board-graph/routing-review/v1",
                "board_sha256": sha1,
                "reviewer": "gemini-4-neon",
                "answer": "PAD J1.1: centered.\nPAD J2.1: centered.\nNOTHING.",
                "earlier": [],
                "warnings": [],
            },
        )
        summary = {"tasks": 1, "kept": 1, "stopped": "", "provider": "gemini", "review_provider": "gemini", "autoroute_steps": 1}
        # Legacy receipts recorded before v0.30.0 remain adoptable without expected_tasks re-parsing Mode: autoroute.
        self.assertEqual(routing_evidence.adoption_problem([ar_step], summary, sha1, "gemini"), "")

        bad_step = dict(ar_step, autoroute={})
        self.assertEqual(
            routing_evidence.adoption_problem([bad_step], summary, sha1, "gemini"),
            "Autoroute step receipt is missing solver provenance",
        )
        note_text = routing_run.note([ar_step], summary, [], "attempt1", "", "gemini")
        self.assertIn("Autoroute steps 1 (kicad-routing-tools@v0.24.1).", note_text)


class AdoptFromTests(unittest.TestCase):
    def _build_fixture(self, *, board_text=None):
        resources = ExitStack()
        self.addCleanup(resources.close)
        root = pathlib.Path(resources.enter_context(tempfile.TemporaryDirectory())).resolve()
        board = root / routing_run.BOARD
        board.parent.mkdir(parents=True)
        rel = "board/routing/tasks"
        tasks = root / rel
        tasks.mkdir(parents=True)
        (tasks / "first").write_text("Repair: change copper")
        (tasks / "second").write_text("Repair: change copper")
        src_board = board_text or BOARD.replace("(at 0 0 0)", "(at 0 8.6 450)")
        board.write_bytes(src_board.encode("utf-8"))

        bg = routing_run.board_graph
        data = json.loads((FIXTURES / "board_graph.json").read_bytes())
        card = dict(next(n for n in data["nodes"] if n["id"] == routing_run.NODE))
        card.update(action="routing_run", docs=[], owns=[routing_run.BOARD], requires=[], outcomes={"pass": "done", "returned": []})
        card.pop("unchanged_pass", None)
        graph_file = root / "graph.json"
        data.update(approval_mode="enforce", docs=[], open_issues=[], nodes=[card],
                    tracks=[dict(id="board", label="Board", ordered=True, start=routing_run.NODE)])
        graph_file.write_text(json.dumps(data))
        (root / "AGENTS.md").write_text("Routing test instructions")
        (root / routing_run.DESCRIPTION.rel("kicad_project")).write_text("{}")

        branch = "gemini/strip"
        local_git = routing_run.routing_delivery.git
        remote = pathlib.Path(resources.enter_context(tempfile.TemporaryDirectory())).resolve()
        local_git(remote, "init", "--bare", "-q")
        local_git(root, "init", "-q", "-b", branch)
        for key, value in (("user.name", "Test"), ("user.email", "test@example.test"), ("core.autocrlf", "false")):
            local_git(root, "config", key, value)
        local_git(root, "remote", "add", "origin", str(remote))
        local_git(root, "add", ".")
        local_git(root, "commit", "-q", "-m", "initial")
        prior_commit = local_git(root, "rev-parse", "HEAD")

        # Add a subsequent needs_lead receipt commit (board and tasks unchanged).
        (root / "board/routing/run-prior123.jsonl").write_text('{"needs_lead": true}\n')
        local_git(root, "add", "board/routing/run-prior123.jsonl")
        local_git(root, "commit", "-q", "-m", "Record failed attempt")
        local_git(root, "push", "-q", "origin", branch)
        local_git(root, "update-ref", "refs/remotes/origin/main", "HEAD")

        graph = REAL_GRAPH(graph_file)
        outside = pathlib.Path(resources.enter_context(tempfile.TemporaryDirectory())).resolve()
        state = bg.State(outside / "db", root / "records")
        return root, branch, rel, tasks, graph_file, graph, state, prior_commit, src_board

    def _prior_outputs(self, branch, rel, prior_commit, prior_id, download):
        digest = hashlib.sha256(download).hexdigest()
        proof = dict(
            provider="gemini", review_provider="gemini", review_model="gemini-4-neon-low",
            reviewed_sha256=digest,
            review_receipt=dict(schema="board-graph/routing-review/v1", board_sha256=digest,
                                reviewer="gemini-4-neon-low", answer="NOTHING.", earlier=[], warnings=[]),
        )
        s1 = dict(step("first", True, used=100), **proof)
        s2 = dict(step("second", True, used=100), **proof)
        summary = dict(SUMMARY, kept=2, stopped="every task was kept")
        meta = {
            "attempt": prior_id, "branch": branch, "commit": prior_commit, "tasks": rel,
            "provider": "gemini", "review_provider": "gemini", "model": "gemini-4-neon-medium",
            "review_model": "gemini-4-neon-low", "tool": {"version": "0.24.2"},
        }
        out_text = "\n".join(map(json.dumps, (s1, s2, summary)))
        return meta, out_text

    def test_adopt_from_recovers_angle_normalized_run_and_passes_copper_origin(self):
        import board_copper_origin
        root, branch, rel, tasks, graph_file, graph, state, prior_commit, src_board = self._build_fixture()
        download = src_board.replace("(at 0 8.6 450)", "(at 0 8.6 90)").replace("(end 2 2)", "(end 4 4)").encode("utf-8")
        prior_id = "prior1234567890abcdef"
        meta, out_text = self._prior_outputs(branch, rel, prior_commit, prior_id, download)
        prior_state = {"state": "finished", "phase": "done", "exit": 0}

        def fetch(path):
            if path.endswith("/run.json"):
                return json.dumps(meta).encode("utf-8")
            if path.endswith("/out.jsonl"):
                return out_text.encode("utf-8")
            if path.endswith("/" + routing_run.BOARD):
                return download
            raise AssertionError(f"unexpected fetch {path}")

        args = argparse.Namespace(
            tasks=tasks, attempt=None, adopt_from=prior_id, provider="gemini", review_provider=None,
            model=None, review_model=None, effort=None, agent=None, by="agent:gemini", note="",
            graph=graph_file, mac_tree="~/tree", mac_steps="~/steps",
        )
        bg = routing_run.board_graph
        with (mock.patch.object(routing_run, "ROOT", root),
              mock.patch.object(routing_run, "engine", return_value=(graph, state, dict(how="ssh", path="fixture"))),
              mock.patch.object(bg, "run_tidy", return_value={"code": 0, "said": "ok\n", "mac": None}),
              mock.patch.object(routing_run, "status", return_value=prior_state),
              mock.patch.object(routing_run, "fetch", side_effect=fetch),
              mock.patch.object(routing_run, "prepare_mac", side_effect=AssertionError("prepare_mac ran")),
              mock.patch.object(routing_run, "launch", side_effect=AssertionError("launch ran")),
              mock.patch.object(routing_run, "wait", side_effect=AssertionError("wait ran")),
              mock.patch.object(routing_run.mac, "ssh", return_value=mock.Mock(returncode=0, stdout=b"abc")),
              mock.patch.multiple(bg, Graph=REAL_GRAPH, record=REAL_RECORD, check_routing_terms=REAL_TERMS),
              mock.patch.object(bg, "keep_pull_request", return_value=""),
              redirect_stdout(StringIO())):
            self.assertEqual(routing_run.run(args), 0)

        self.assertEqual((root / routing_run.BOARD).read_bytes(), download)
        row, = state.transitions()
        self.assertEqual(row["outcome"], "more_work")
        self.assertEqual(row["inputs"].get("tokens", 0), 0)
        new_attempt = row["inputs"]["attempt_id"]
        self.assertNotEqual(new_attempt, prior_id)
        receipt = root / f"board/routing/run-{new_attempt[:8]}.jsonl"
        summary_line = json.loads(receipt.read_text(encoding="utf-8").splitlines()[-1])
        self.assertTrue(summary_line["adopted"])
        self.assertEqual(summary_line["recovered_from"], prior_id)
        self.assertEqual(summary_line["tokens"], 2420)
        self.assertIn(f"Recovered from verified run {prior_id[:8]}.", row["note"])
        self.assertEqual(board_copper_origin.unaccounted(root, routing_run.BOARD, state.records, base=prior_commit), [])

    def test_adopt_from_refuses_without_opening_attempt_on_bad_state(self):
        root, branch, rel, tasks, graph_file, graph, state, prior_commit, src_board = self._build_fixture()
        clean_download = src_board.replace("(end 2 2)", "(end 4 4)").encode("utf-8")
        prior_id = "prior1234567890abcdef"
        base_meta, base_out = self._prior_outputs(branch, rel, prior_commit, prior_id, clean_download)
        moved_download = clean_download.replace(b"(at 10 20 90)", b"(at 10.5 20 90)")
        moved_meta, moved_out = self._prior_outputs(branch, rel, prior_commit, prior_id, moved_download)

        cases = [
            ("not_finished", {"state": "running"}, base_meta, base_out, clean_download, "not finished"),
            ("wrong_branch", {"state": "finished"}, dict(base_meta, branch="other"), base_out, clean_download, "different attempt, branch"),
            ("bad_review", {"state": "finished"}, base_meta, base_out, clean_download + b"\n", "Downloaded PCB differs from the independently reviewed"),
            ("footprint_moved", {"state": "finished"}, moved_meta, moved_out, moved_download, "footprint R36 changed"),
        ]

        args = argparse.Namespace(
            tasks=tasks, attempt=None, adopt_from=prior_id, provider="gemini", review_provider=None,
            model=None, review_model=None, effort=None, agent=None, by="agent:gemini", note="",
            graph=graph_file, mac_tree="~/tree", mac_steps="~/steps",
        )
        for label, prior_state, meta, out_text, download, expected_err in cases:
            with self.subTest(case=label):
                def fetch(path, _m=meta, _o=out_text, _d=download):
                    if path.endswith("/run.json"):
                        return json.dumps(_m).encode("utf-8")
                    if path.endswith("/out.jsonl"):
                        return _o.encode("utf-8")
                    return _d

                with (mock.patch.object(routing_run, "ROOT", root),
                      mock.patch.object(routing_run, "engine", return_value=(graph, state, dict(how="ssh", path="fixture"))),
                      mock.patch.object(routing_run, "status", return_value=prior_state),
                      mock.patch.object(routing_run, "fetch", side_effect=fetch),
                      mock.patch.object(routing_run, "start") as start_mock):
                    with self.assertRaisesRegex(routing_run.Refused, expected_err):
                        routing_run.run(args)
                    start_mock.assert_not_called()

        # Tasks changed after prior_commit refuses before opening attempt.
        (tasks / "first").write_text("Repair: modified task")
        local_git = routing_run.routing_delivery.git
        local_git(root, "add", str(tasks / "first"))
        local_git(root, "commit", "-q", "-m", "Edit task")
        local_git(root, "push", "-q", "origin", branch)
        local_git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
        with (mock.patch.object(routing_run, "ROOT", root),
              mock.patch.object(routing_run, "engine", return_value=(graph, state, dict(how="ssh", path="fixture"))),
              mock.patch.object(routing_run, "status", return_value={"state": "finished"}),
              mock.patch.object(routing_run, "fetch", side_effect=lambda p: json.dumps(base_meta).encode() if p.endswith("/run.json") else base_out.encode() if p.endswith("/out.jsonl") else clean_download),
              mock.patch.object(routing_run, "start") as start_mock):
            with self.assertRaisesRegex(routing_run.Refused, "task files in board/routing/tasks changed"):
                routing_run.run(args)
            start_mock.assert_not_called()

        # Non-ancestor prior commit refuses before opening attempt.
        other_meta = dict(base_meta, commit="f" * 40)
        with (mock.patch.object(routing_run, "ROOT", root),
              mock.patch.object(routing_run, "engine", return_value=(graph, state, dict(how="ssh", path="fixture"))),
              mock.patch.object(routing_run, "status", return_value={"state": "finished"}),
              mock.patch.object(routing_run, "fetch", side_effect=lambda p: json.dumps(other_meta).encode() if p.endswith("/run.json") else base_out.encode() if p.endswith("/out.jsonl") else clean_download),
              mock.patch.object(routing_run, "start") as start_mock):
            with self.assertRaisesRegex(routing_run.Refused, "not an ancestor"):
                routing_run.run(args)
            start_mock.assert_not_called()


class PourSupportTests(unittest.TestCase):
    def test_parse_task_pours_and_preflight(self):
        import pad_exits
        import routing_evidence

        task = "Mode: ipc\nPour: In1.Cu GND\nPour: F.Cu,B.Cu +5V\n\nPour both planes.\n"
        self.assertEqual(
            routing_evidence.parse_task_pours(task),
            [{"layers": ["In1.Cu"], "net": "GND"}, {"layers": ["F.Cu", "B.Cu"], "net": "+5V"}],
        )
        self.assertEqual(routing_evidence.parse_task_connections(task), [])

        with self.assertRaises(ValueError):
            routing_evidence.parse_task_pours("Pour: F.Cu GND\nPour: F.Cu GND\n")
        with self.assertRaises(ValueError):
            routing_evidence.parse_task_pours("Pour: *.Cu GND\n")
        with self.assertRaises(ValueError):
            routing_evidence.parse_task_pours("Text first\nPour: In1.Cu GND\n")

        pcb = """(kicad_pcb
          (layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (31 "B.Cu" signal))
          (net 0 "")
          (net 1 "GND")
          (net 2 "+5V")
        )"""
        self.assertEqual(pad_exits.preflight_tasks(pcb, [("01-pour.txt", task)]), [])
        probs = pad_exits.preflight_tasks(pcb, [("01-bad.txt", "Pour: In2.Cu NO_SUCH_NET\n")])
        self.assertEqual(len(probs), 2)
        self.assertIn("not enabled", probs[0])
        self.assertIn("not on this board", probs[1])

    def test_saved_pours_and_adoption_problem_single_and_multi_step(self):
        import routing_evidence

        pcb = """(kicad_pcb
          (version 20260206)
          (layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal) (31 "B.Cu" signal))
          (net 0 "")
          (net 1 "GND")
          (net 2 "+5V")
          (zone (net 0) (layers "In1.Cu" "In2.Cu") (name "ko")
            (keepout (copperpour not_allowed))
            (polygon (pts (xy 1 1) (xy 4 1) (xy 4 4) (xy 1 4)))
            (filled_polygon (layer "In1.Cu") (pts (xy 1 1) (xy 4 1) (xy 4 4)))
          )
          (zone (net 1) (net_name "GND") (layers "In1.Cu")
            (polygon (pts (xy 0 0) (xy 20 0) (xy 20 20) (xy 0 20)))
            (filled_polygon (layer "In1.Cu") (pts (xy 0 0) (xy 20 0) (xy 20 20)))
          )
          (zone (net "+5V") (layers "F.Cu" "B.Cu")
            (polygon (pts (xy 0 0) (xy 20 0) (xy 20 20) (xy 0 20)))
            (filled_polygon (layer "F.Cu") (pts (xy 0 0) (xy 20 0) (xy 20 20)))
            (filled_polygon (layer "B.Cu") (pts (xy 0 0) (xy 20 0) (xy 20 20)))
          )
        )"""
        sha1 = hashlib.sha256(pcb.encode("utf-8")).hexdigest()
        res = routing_evidence.saved_pours(pcb, [{"layers": ["In1.Cu"], "net": "GND"}, {"layers": ["F.Cu", "B.Cu"], "net": "+5V"}], sha1)
        self.assertTrue(all(r["filled"] and r["zone_count"] == 1 for r in res))
        self.assertEqual((res[0]["filled_polygons"], res[1]["filled_polygons"]), (1, 2))

        # Missing fill on one requested layer must report filled=False (and keepout must be ignored)
        unfilled_in2 = routing_evidence.saved_pours(pcb, [{"layers": ["In2.Cu"], "net": "GND"}], sha1)
        self.assertFalse(unfilled_in2[0]["filled"])
        self.assertEqual(unfilled_in2[0]["zone_count"], 0)

        sha2 = hashlib.sha256(b"final-board").hexdigest()
        task1 = "Mode: ipc\nPour: In1.Cu GND\nPour: F.Cu,B.Cu +5V\n"
        task2 = "Mode: ipc\nPour: In2.Cu GND\n"
        proof1 = {"schema": "board-graph/routing-review/v1", "board_sha256": sha1, "reviewer": "gemini-4-neon-low",
                  "answer": "NOTHING.", "warnings": [], "earlier": []}
        proof2 = {"schema": "board-graph/routing-review/v1", "board_sha256": sha2, "reviewer": "gemini-4-neon-low",
                  "answer": "NOTHING.", "warnings": [], "earlier": []}
        step1 = {
            "task": "01-pour.txt", "kept": True, "why": "editor_work", "routing_mode": "ipc",
            "provider": "gemini", "review_provider": "gemini", "model": "gemini-4-neon-medium", "review_model": "gemini-4-neon-low",
            "reviewed_sha256": sha1, "reviews": 1, "review_open": [], "review_receipt": proof1,
            "pours": res,
        }
        step2 = {
            "task": "02-pour.txt", "kept": True, "why": "editor_work", "routing_mode": "ipc",
            "provider": "gemini", "review_provider": "gemini", "model": "gemini-4-neon-medium", "review_model": "gemini-4-neon-low",
            "reviewed_sha256": sha2, "reviews": 1, "review_open": [], "review_receipt": proof2,
            "pours": [{"layers": ["In2.Cu"], "net": "GND", "zone_count": 1, "filled": True, "filled_polygons": 1, "saved_sha256": sha2}],
        }
        summary = {
            "tasks": 2, "kept": 2, "stopped": "", "provider": "gemini", "review_provider": "gemini",
            "final_pours": {"board_sha256": sha2, "pours": {"01-pour.txt": [{"layers": ["In1.Cu"], "net": "GND"}, {"layers": ["F.Cu", "B.Cu"], "net": "+5V"}]}},
        }
        expected = {"01-pour.txt": task1, "02-pour.txt": task2}
        self.assertEqual(routing_evidence.adoption_problem([step1, step2], summary, sha2, "gemini", expected_tasks=expected), "")

        bad_summary = dict(summary, final_pours=None)
        self.assertIn("Earlier named pours", routing_evidence.adoption_problem([step1, step2], bad_summary, sha2, "gemini", expected_tasks=expected))

        wrong_task_step = dict(step1, pours=[{"layers": ["In1.Cu"], "net": "+5V", "zone_count": 1, "filled": True, "filled_polygons": 1, "saved_sha256": sha1}])
        self.assertIn("assigned layers and net", routing_evidence.adoption_problem([wrong_task_step], {"tasks": 1, "kept": 1, "stopped": "", "provider": "gemini"}, sha1, "gemini", expected_tasks={"01-pour.txt": task1}))

    def test_copper_origin_accepts_adopted_pour_commit(self):
        import subprocess
        import board_copper_origin

        pcb_rel = "board/demo.kicad_pcb"
        task_rel = "tasks_pour/01-pour.txt"
        task_text = "Mode: ipc\nPour: In1.Cu GND\n"
        base_pcb = "(kicad_pcb\n  (layers (0 \"F.Cu\" signal) (1 \"In1.Cu\" signal) (31 \"B.Cu\" signal))\n  (net 1 \"GND\")\n)\n"
        poured_pcb = (
            "(kicad_pcb\n  (layers (0 \"F.Cu\" signal) (1 \"In1.Cu\" signal) (31 \"B.Cu\" signal))\n  (net 1 \"GND\")\n"
            "  (zone (net 1) (net_name \"GND\") (layers \"In1.Cu\")\n"
            "    (polygon (pts (xy 0 0) (xy 20 0) (xy 20 20) (xy 0 20)))\n"
            "    (filled_polygon (layer \"In1.Cu\") (pts (xy 0 0) (xy 20 0) (xy 20 20)))\n  )\n)\n"
        )
        with tempfile.TemporaryDirectory() as td:
            repo = pathlib.Path(td)
            git = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
            git("init", "-q", "-b", "main")
            git("config", "user.email", "t@example.com")
            git("config", "user.name", "t")
            git("config", "commit.gpgsign", "false")
            (repo / "board").mkdir()
            (repo / "tasks_pour").mkdir()
            (repo / pcb_rel).write_bytes(base_pcb.encode("utf-8"))
            (repo / task_rel).write_bytes(task_text.encode("utf-8"))
            git("add", "-A")
            git("commit", "-q", "-m", "base")
            git("update-ref", "refs/remotes/origin/main", "HEAD")
            git("checkout", "-q", "-b", "work")
            start_commit = git("rev-parse", "HEAD")

            digest = hashlib.sha256(poured_pcb.encode("utf-8")).hexdigest()
            proof = {"schema": "board-graph/routing-review/v1", "board_sha256": digest, "reviewer": "gemini-4-neon-low",
                     "answer": "NOTHING.", "warnings": [], "earlier": []}
            step_rec = {
                "task": "01-pour.txt", "kept": True, "why": "editor_work", "routing_mode": "ipc",
                "provider": "gemini", "review_provider": "gemini", "model": "gemini-4-neon-medium", "review_model": "gemini-4-neon-low",
                "reviewed_sha256": digest, "reviews": 1, "review_open": [], "review_receipt": proof,
                "pours": [{"layers": ["In1.Cu"], "net": "GND", "zone_count": 1, "filled": True, "filled_polygons": 1, "saved_sha256": digest}],
            }
            summary_rec = {"summary": True, "adopted": True, "outcome": "more_work", "pcb": pcb_rel, "kept": 1, "tasks": 1,
                           "provider": "gemini", "review_provider": "gemini", "commit": start_commit, "task_folder": "tasks_pour"}
            (repo / pcb_rel).write_bytes(poured_pcb.encode("utf-8"))
            (repo / "board" / "run-pour1234.jsonl").write_bytes((json.dumps(step_rec) + "\n" + json.dumps(summary_rec) + "\n").encode("utf-8"))
            git("add", "-A")
            git("commit", "-q", "-m", "Routing run pour1234: 1 of 1 steps kept")
            self.assertEqual(board_copper_origin.unaccounted(repo, pcb_rel, repo / "records"), [])

    def test_passive_run_profile_in_evidence_format_profile_and_kept_note(self):
        steps = [
            {
                **step("01-pour", True, used=100),
                "timing": {"total_s": 120.0, "llm_s": 90.0, "tool_s": 20.0, "review_s": 8.0,
                           "harness_s": 2.0, "refusal_s": 4.0, "thinking_tokens": 3000},
                "calls": {
                    "route": 0, "route_closed": 0, "route_refused": 0,
                    "lay": 3, "lay_laid": 1, "lay_refused": 1, "edit": 0, "look": 1, "other": 0,
                    "help_calls": 1, "refusal_seconds": 4.0,
                    "refusal_reasons": {"kicad_lay: bad_request: need to_via": {"count": 1, "seconds": 4.0}},
                },
            },
            {
                **step("02-gnd", True, used=200),
                "timing": {"total_s": 80.0, "llm_s": 55.0, "tool_s": 15.0, "review_s": 9.0,
                           "harness_s": 1.0, "refusal_s": 2.0, "thinking_tokens": 1500},
                "calls": {
                    "route": 0, "route_closed": 0, "route_refused": 0,
                    "lay": 2, "lay_laid": 1, "lay_refused": 1, "edit": 0, "look": 1, "other": 0,
                    "help_calls": 0, "refusal_seconds": 2.0,
                    "refusal_reasons": {"kicad_lay: bad_request: need to_via": {"count": 1, "seconds": 2.0}},
                },
            },
        ]
        summary = {
            "stopped": "", "kept": 2, "tasks": 2,
            "tokens_total": {"input": 300, "output": 20, "cache_read": 2000, "cache_write": 200},
            "route_calls": 0, "route_closed": 0, "route_refused": 0,
            "lay_calls": 5, "lay_laid": 2, "lay_refused": 2,
            "timing": {"total_s": 200.0, "llm_s": 145.0, "tool_s": 35.0, "review_s": 17.0,
                       "harness_s": 3.0, "refusal_s": 6.0, "thinking_tokens": 4500},
            "refusals": {
                "help_calls": 1, "refusal_seconds": 6.0,
                "by_reason": {"kicad_lay: bad_request: need to_via": {"count": 2, "seconds": 6.0, "steps": 2}},
                "repeated_across_steps": [{"reason": "kicad_lay: bad_request: need to_via", "steps": 2, "count": 2, "seconds": 6.0}],
            },
        }
        # Zero gates: outcome stays more_work despite refusals/help calls.
        self.assertEqual(routing_run.outcome(steps, summary, []), "more_work")
        lines = routing_run.evidence_lines(steps, summary, {"attempt": "deadbeef", "adopted": True})
        first = json.loads(lines[0])
        last = json.loads(lines[-1])
        self.assertEqual(first["timing"]["thinking_tokens"], 3000)
        self.assertEqual(last["timing"]["llm_s"], 145.0)
        self.assertEqual(last["refusals"]["help_calls"], 1)

        profile = routing_run.format_profile(steps, summary)
        self.assertIn("Run profile: 200s total (LLM 145s, tools 35s, review 17s, harness 3s; thinking 4,500 tok).", profile)
        self.assertIn("Tool friction: 2 refusal(s) (6.0s), 1 --help call(s).", profile)
        self.assertIn("Repeated across steps: kicad_lay: bad_request: need to_via (2 steps, 2x).", profile)

        text = routing_run.note(steps, summary, [], "deadbeef00")
        self.assertIn("2 of 2 steps kept.", text)
        self.assertIn("Time: 200s (LLM 145s, tools 35s, review 17s; thinking 4,500).", text)
        self.assertIn("Refusals: kicad_lay: bad_request: need to_via (2x).", text)
        self.assertIn("Help calls: 1.", text)
        self.assertIn("Repeated across steps: kicad_lay: bad_request: need to_via (2 steps, 2x).", text)


class V27AdoptAndProfileTests(unittest.TestCase):
    def test_empty_zone_defaults_ignored_in_setup_while_non_empty_flagged(self) -> None:
        import sys
        import types
        from unittest import mock

        clean = """(kicad_pcb
  (version 20260206)
  (setup (pad_to_mask_clearance 0))
)"""
        with_empty = """(kicad_pcb
  (version 20260206)
  (setup (pad_to_mask_clearance 0) (zone_defaults))
)"""
        with_setting = """(kicad_pcb
  (version 20260206)
  (setup (pad_to_mask_clearance 0) (zone_defaults (thermal_gap 0.3)))
)"""
        self.assertEqual(routing_run.beyond_copper(clean)[0], routing_run.beyond_copper(with_empty)[0])
        self.assertEqual(routing_run.guard(clean, with_empty), [])
        self.assertNotEqual(routing_run.beyond_copper(clean)[0], routing_run.beyond_copper(with_setting)[0])
        self.assertEqual(routing_run.guard(clean, with_setting), ["setup changed"])

        fake_kipy = types.SimpleNamespace(KiCad=object)
        with mock.patch.dict(sys.modules, {"kipy": fake_kipy}):
            import importlib
            import kicad_reload_board

            importlib.reload(kicad_reload_board)
            self.assertEqual(kicad_reload_board.board_items(clean), kicad_reload_board.board_items(with_empty))
            self.assertNotEqual(kicad_reload_board.board_items(clean), kicad_reload_board.board_items(with_setting))

    def test_format_profile_groups_normalized_cross_step_refusals(self) -> None:
        steps = [
            {
                "calls": {
                    "help_calls": 0,
                    "refusal_seconds": 12.0,
                    "refusal_reasons": {
                        "kicad_lay: bad_request: U2.6: neither short edge faces (143.137, 101.125)": {
                            "count": 2,
                            "seconds": 12.0,
                        }
                    },
                }
            },
            {
                "calls": {
                    "help_calls": 1,
                    "refusal_seconds": 6.5,
                    "refusal_reasons": {
                        "kicad_lay: bad_request: U2.5: neither short edge faces (143.137, 100.475)": {
                            "count": 1,
                            "seconds": 6.5,
                        }
                    },
                }
            },
        ]
        _timing, refusals = routing_run._collect_profile(steps, None)
        self.assertIsNotNone(refusals)
        self.assertEqual(len(refusals["by_reason"]), 2)
        self.assertIn("kicad_lay: bad_request: U2.6: neither short edge faces (143.137, 101.125)", refusals["by_reason"])
        self.assertIn("kicad_lay: bad_request: U2.5: neither short edge faces (143.137, 100.475)", refusals["by_reason"])
        self.assertEqual(
            refusals["repeated_across_steps"],
            [{
                "reason": "kicad_lay: bad_request: <pad>: neither short edge faces (<x,y>)",
                "steps": 2,
                "count": 3,
                "seconds": 18.5,
            }],
        )
        profile = routing_run.format_profile(steps, None)
        self.assertIn("Repeated across steps: kicad_lay: bad_request: <pad>: neither short edge faces (<x,y>) (2 steps, 3x).", profile)


class V28ProfileExtraToolsTests(unittest.TestCase):
    def test_format_profile_prints_extra_tools_only_when_present(self) -> None:
        steps = [
            {"calls": {"edit_by_verb": {"list": 3, "delete": 1}, "other_by_cmd": {"python3": 1}}},
            {"calls": {"edit_by_verb": {"list": 2, "delete": 1}, "other_by_cmd": {"grep": 1, "python3": 1}}},
        ]
        text = routing_run.format_profile(steps, None)
        self.assertEqual(text, "Extra tool calls: kicad_edit (list=5, delete=2); other (python3=2, grep=1).")

        empty_text = routing_run.format_profile([{"calls": {"edit_by_verb": {}, "other_by_cmd": {}}}], None)
        self.assertEqual(empty_text, "")


class MultiPadConnectionTests(unittest.TestCase):
    def test_preflight_and_board_context_support_non_centered_multipad_switches(self) -> None:
        import board_context
        import pad_exits

        pcb_ok = """(kicad_pcb
          (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
          (net 0 "")
          (net 1 "/BTN_UP")
          (footprint "SW" (layer "F.Cu") (at 10 20)
            (property "Reference" "SW1")
            (pad "1" smd rect (at 0 -1.5) (size 1.0 0.8) (layers "F.Cu") (net 1 "/BTN_UP"))
            (pad "1" smd rect (at 0 1.5) (size 1.0 0.8) (layers "F.Cu") (net 1 "/BTN_UP"))
          )
          (footprint "SW" (layer "F.Cu") (at 10 40)
            (property "Reference" "SW2")
            (pad "1" smd rect (at 0 -1.5) (size 1.0 0.8) (layers "F.Cu") (net 1 "/BTN_UP"))
            (pad "1" smd rect (at 0 1.5) (size 1.0 0.8) (layers "F.Cu") (net 1 "/BTN_UP"))
          )
          (footprint "U" (layer "F.Cu") (at 30 20)
            (property "Reference" "U1")
            (pad "26" smd rect (at 0 0) (size 0.3 1.2) (layers "F.Cu") (net 1 "/BTN_UP"))
          )
        )"""
        task = "Mode: ipc\nConnection: U1.26 SW1.1 /BTN_UP\n"
        self.assertEqual(pad_exits.preflight_tasks(pcb_ok, [("01-btn.txt", task)]), [])
        self.assertEqual(pad_exits.preflight_tasks(pcb_ok, [("02-sw.txt", "Mode: ipc\nConnection: SW1.1 SW2.1 /BTN_UP\n")]), [])

        with tempfile.TemporaryDirectory() as td:
            pcb_path = pathlib.Path(td) / "sw.kicad_pcb"
            pcb_path.write_text(pcb_ok, encoding="utf-8")
            inline_nc, _ = board_context.render_board_context(pcb_path, [["U1.26", "SW1.1", "/BTN_UP"]], centered=False)
            self.assertIn("SW1.1@10,18.5", inline_nc)
            self.assertNotIn("SW1.1 (not found)", inline_nc)
            inline_c, _ = board_context.render_board_context(pcb_path, [["U1.26", "SW1.1", "/BTN_UP"]], centered=True)
            self.assertIn("SW1.1 (not found)", inline_c)

        task_centered = "Mode: ipc\nConnection: U1.26 SW1.1 /BTN_UP\nPad exits: centered\n"
        probs_centered = pad_exits.preflight_tasks(pcb_ok, [("01-btn.txt", task_centered)])
        self.assertEqual(len(probs_centered), 1)
        self.assertIn("centered exits require a single pad", probs_centered[0])

        pcb_bad_net = pcb_ok.replace(
            '(pad "1" smd rect (at 0 1.5) (size 1.0 0.8) (layers "F.Cu") (net 1 "/BTN_UP"))',
            '(pad "1" smd rect (at 0 1.5) (size 1.0 0.8) (layers "F.Cu") (net 0 ""))',
            1,
        )
        probs_bad = pad_exits.preflight_tasks(pcb_bad_net, [("01-btn.txt", task)])
        self.assertEqual(len(probs_bad), 1)
        self.assertIn("expected '/BTN_UP'", probs_bad[0])

    def test_kicad_check_connection_requires_all_pads_in_multipad_group(self) -> None:
        import kicad_check_connection

        class FakeUuid:
            def __init__(self, val):
                self._val = val
            def AsString(self):
                return self._val

        class FakePad:
            def __init__(self, ref, num, net, uid):
                self._ref, self._num, self._net = ref, num, net
                self.m_Uuid = FakeUuid(uid)
            def GetParentFootprint(self):
                return type("FP", (), {"GetReference": lambda s: self._ref})()
            def GetNumber(self):
                return self._num
            def GetNetname(self):
                return self._net

        u_pad = FakePad("U1", "26", "/BTN_UP", "u26")
        sw_a = FakePad("SW1", "1", "/BTN_UP", "sw1a")
        sw_b = FakePad("SW1", "1", "/BTN_UP", "sw1b")

        class FakeBoard:
            def __init__(self, edges, pads=(u_pad, sw_a, sw_b)):
                self._edges = edges
                self._pads = list(pads)
            def GetPads(self):
                return self._pads
            def BuildConnectivity(self):
                pass
            def GetConnectivity(self):
                edges = self._edges
                def get_connected(item):
                    uid = item.m_Uuid.AsString()
                    seen, stack = {uid}, [uid]
                    while stack:
                        cur = stack.pop()
                        for nxt in edges.get(cur, ()):
                            if nxt not in seen:
                                seen.add(nxt)
                                stack.append(nxt)
                    return [type("I", (), {"m_Uuid": FakeUuid(i)})() for i in seen if i != uid]
                return type("Conn", (), {"GetConnectedItems": staticmethod(get_connected)})()

        self.assertFalse(kicad_check_connection.connected(FakeBoard({"u26": ["sw1a"], "sw1a": ["u26"]}), "U1.26", "SW1.1", "/BTN_UP"))
        # Anchor SW1.1 first: u26 reaches sw1a, but sw1b is only connected when bridged.
        self.assertFalse(kicad_check_connection.connected(FakeBoard({"sw1a": ["u26"], "u26": ["sw1a"]}), "SW1.1", "U1.26", "/BTN_UP"))
        full_edges = {"u26": ["sw1a"], "sw1a": ["u26", "sw1b"], "sw1b": ["sw1a"]}
        self.assertTrue(kicad_check_connection.connected(FakeBoard(full_edges), "U1.26", "SW1.1", "/BTN_UP"))
        self.assertTrue(kicad_check_connection.connected(FakeBoard(full_edges), "SW1.1", "U1.26", "/BTN_UP"))
        for args in (("U1.26", "U1.26", "/BTN_UP"), ("U1.26", "SW1.9", "/BTN_UP"), ("U1.26", "SW1.1", "/OTHER")):
            with self.assertRaises(ValueError):
                kicad_check_connection.named_pad_groups(FakeBoard(full_edges), *args)
        with self.assertRaises(ValueError):
            kicad_check_connection.named_pads(FakeBoard(full_edges), "U1.26", "SW1.1", "/BTN_UP")


class KiCad10SaveNormalizationTests(unittest.TestCase):
    def test_guard_and_reload_accept_kicad8_to_10_serialization_upgrade_and_catch_real_diffs(self) -> None:
        import importlib
        import types

        k8 = """(kicad_pcb
  (version 20241229)
  (generator "diy")
  (setup
    (stackup (layer "F.Cu" (type "copper")))
    (pcbplotparams (layerselection 0x1))
  )
  (net 0 "")
  (net 1 "VBUS")
  (net 2 "GND")
  (footprint "lib:C" (layer "F.Cu") (at 10 20 90)
    (property "Reference" "C1" (at 0 -1 90) (layer "F.SilkS"))
    (fp_line (start -1 -1) (end 1 -1) (layer "F.SilkS"))
    (fp_text user "${REFERENCE}" (at 0 0 90) (layer "F.Fab") (effects (font (size 0.4 0.4))) (uuid "u1"))
    (pad "1" smd roundrect (at -0.7 0 90) (size 0.9 0.9) (layers "F.Cu") (net 1 "VBUS"))
    (pad "2" smd roundrect (at 0.7 0 90) (size 0.9 0.9) (layers "F.Cu") (net 2 "GND"))
  )
)"""
        k10 = """(kicad_pcb
  (version 20260206)
  (generator "pcbnew")
  (setup
    (stackup (layer "F.Cu" (type "copper")))
    (zone_defaults)
    (pcbplotparams (layerselection 0x1) (disableapertmacros no) (mirror no))
  )
  (embedded_fonts no)
  (footprint "lib:C" (layer "F.Cu") (at 10 20 90)
    (property "Reference" "C1" (at 0 -1 90) (layer "F.SilkS"))
    (pad "2" smd roundrect (at 0.7 0 90) (size 0.9 0.9) (layers "F.Cu") (net "GND"))
    (fp_text user "${REFERENCE}" (at 0 0 90) (layer "F.Fab") (uuid "u1") (effects (font (size 0.4 0.4))))
    (fp_line (start -1 -1) (end 1 -1) (layer "F.SilkS"))
    (pad "1" smd roundrect (at -0.7 0 90) (size 0.9 0.9) (layers "F.Cu") (net "VBUS"))
    (embedded_fonts no)
  )
)"""
        self.assertEqual(routing_run.guard(k8, k10), [])
        fake_kipy = types.SimpleNamespace(KiCad=object)
        with mock.patch.dict(sys.modules, {"kipy": fake_kipy}):
            import kicad_reload_board
            importlib.reload(kicad_reload_board)
            self.assertEqual(kicad_reload_board.board_items(k8), kicad_reload_board.board_items(k10))

            bad_net = k10.replace('(net "VBUS")', '(net "GND")', 1)
            self.assertEqual(routing_run.guard(k8, bad_net), ["footprint C1 changed"])
            self.assertNotEqual(kicad_reload_board.board_items(k8), kicad_reload_board.board_items(bad_net))

            bad_fonts = k10.replace("(embedded_fonts no)", "(embedded_fonts yes)", 1)
            self.assertIn("embedded_fonts added", routing_run.guard(k8, bad_fonts))

            bad_stackup = k10.replace('(layer "F.Cu" (type "copper"))', '(layer "F.Cu" (type "copper") (thickness 0.07))')
            self.assertEqual(routing_run.guard(k8, bad_stackup), ["setup changed"])

            bad_fp_move = k10.replace("(at 10 20 90)", "(at 11 20 90)")
            self.assertEqual(routing_run.guard(k8, bad_fp_move), ["footprint C1 changed"])

            # Edge case 1: pad renamed to literal net "1" when (net 1 "VBUS") exists in net_map must NOT mask change.
            bad_num_net = k10.replace('(net "VBUS")', '(net "1")', 1)
            self.assertEqual(routing_run.guard(k8, bad_num_net), ["footprint C1 changed"])
            self.assertNotEqual(kicad_reload_board.board_items(k8), kicad_reload_board.board_items(bad_num_net))

        # Edge case 2: normalize_angles must run before child repr sorting (SW1 twin pads at 0 vs 90 deg).
        sw_k8 = """(kicad_pcb
  (version 20241229)
  (net 1 "/BTN")
  (footprint "lib:SW" (layer "F.Cu") (at 10 20 0)
    (property "Reference" "SW1" (at 0 -2 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0 0) (size 1 1) (layers "F.Cu") (net 1 "/BTN"))
    (pad "1" smd rect (at 0 0 90) (size 1 1) (layers "F.Cu") (net 1 "/BTN"))
  )
)"""
        sw_k10 = """(kicad_pcb
  (version 20260206)
  (footprint "lib:SW" (layer "F.Cu") (at 10 20)
    (property "Reference" "SW1" (at 0 -2) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0 90) (size 1 1) (layers "F.Cu") (net "/BTN"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net "/BTN"))
  )
)"""
        self.assertEqual(routing_run.guard(sw_k8, sw_k10), [])

    def test_guard_accepts_initial_save_of_unfilled_mechanical_geometry(self):
        import board_graph_actions

        before = '''(kicad_pcb
  (gr_rect (start 0 0) (end 32 35) (fill none) (layer "Edge.Cuts"))
  (footprint "lib:M3" (at 4 4) (layer "F.Cu")
    (property "Reference" "H1" (at 0 0) (layer "F.Fab") (effects (font (size 0.8 0.8)) (hide yes)))
    (fp_circle (center 0 0) (end 3 0) (fill none) (layer "F.CrtYd"))))'''
        after = before.replace("(fill none)", "(fill no)").replace(
            '(effects (font (size 0.8 0.8)) (hide yes))',
            '(hide yes) (effects (font (size 0.8 0.8)))'
            ') (property "Datasheet" "" (at 0 0) (layer "F.Fab") (hide yes)'
        )
        self.assertEqual(routing_run.guard(before, after), [])
        self.assertEqual(board_graph_actions.placement_view(before), board_graph_actions.placement_view(after))
        self.assertEqual(board_graph_actions.purchasing_view(before), board_graph_actions.purchasing_view(after))
        self.assertEqual(routing_run.guard(before, after.replace("(fill no)", "(fill yes)")),
                         ["footprint H1 changed", "graphic gr_rect changed"])
        self.assertEqual(routing_run.guard(before, after.replace(
            '(property "Datasheet" ""', '(property "Datasheet" "https://example.com"')),
            ["footprint H1 changed"])
        self.assertEqual(routing_run.guard(before, after.replace('(hide yes)', '(hide no)', 1)),
                         ["footprint H1 changed"])

    def test_guard_and_reload_accept_kicad9_first_save_metadata_and_catch_real_diffs(self) -> None:
        import importlib
        import types

        k9 = """(kicad_pcb
  (version 20241229)
  (generator "pcbnew")
  (setup
    (stackup (layer "F.Cu" (type "copper")))
  )
  (net 0 "")
  (net 1 "/S1")
  (gr_line (start 1.5 0.0) (end 34.5 0.0) (stroke (width 0.1) (type solid)) (layer "Edge.Cuts"))
  (gr_text "3.0" (at 10.0 20.0 0) (layer "F.SilkS") (effects (font (size 1.0 1.0) (thickness 0.15))))
  (footprint "lib:H" (layer "F.Cu") (at 3.5 3.5) (attr exclude_from_bom exclude_from_pos_files)
    (property "Reference" "H1" (at 0 -3.5 0) (layer "F.Fab") (hide yes) (effects (font (size 1.0 1.0) (thickness 0.15))))
    (property "Footprint" "lib:H" (at 0 0 0) (layer "F.Fab") (hide yes) (effects (font (size 1.0 1.0) (thickness 0.15))))
    (fp_circle (center 0 0) (end 3.0 0.0) (stroke (width 0.15) (type solid)) (fill no) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 1.0 2.0) (layers "F.Cu") (net 1 "/S1"))
  )
)"""
        k10 = """(kicad_pcb
  (version 20260206)
  (generator "pcbnew")
  (generator_version "10.0")
  (setup
    (stackup (layer "F.Cu" (type "copper")))
    (tenting (front yes) (back yes))
    (covering (front no) (back no))
    (plugging (front no) (back no))
    (capping no)
    (filling no)
  )
  (gr_line (start 1.5 0) (end 34.5 0) (stroke (width 0.1) (type solid)) (layer "Edge.Cuts") (uuid "u-edge"))
  (gr_text "3.0" (at 10 20) (layer "F.SilkS") (uuid "u-txt") (effects (font (size 1 1) (thickness 0.15))))
  (footprint "lib:H" (layer "F.Cu") (at 3.5 3.5) (attr exclude_from_pos_files exclude_from_bom) (duplicate_pad_numbers_are_jumpers no)
    (property "Reference" "H1" (at 0 -3.5) (layer "F.Fab") (hide yes) (uuid "u-ref") (effects (font (size 1 1) (thickness 0.15))))
    (property "Description" "" (at 0 0) (layer "F.Fab") (hide yes) (uuid "u-desc") (effects (font (size 1.27 1.27))))
    (fp_circle (center 0 0) (end 3 0) (stroke (width 0.15) (type solid)) (fill no) (layer "F.SilkS") (uuid "u-circ"))
    (pad "1" smd rect (at 0 0) (size 1 2) (layers "F.Cu") (net "/S1") (uuid "u-pad"))
  )
)"""
        self.assertEqual(routing_run.guard(k9, k10), [])
        fake_kipy = types.SimpleNamespace(KiCad=object)
        with mock.patch.dict(sys.modules, {"kipy": fake_kipy}):
            import kicad_reload_board
            importlib.reload(kicad_reload_board)
            self.assertEqual(kicad_reload_board.board_items(k9), kicad_reload_board.board_items(k10))

        self.assertEqual(routing_run.guard(k9, k10.replace("(capping no)", "(capping yes)")), ["setup changed"])
        self.assertEqual(
            routing_run.guard(k9, k10.replace("(tenting (front yes) (back yes))", "(tenting (front no) (back yes))")),
            ["setup changed"],
        )
        self.assertEqual(
            routing_run.guard(k9, k10.replace("(duplicate_pad_numbers_are_jumpers no)", "(duplicate_pad_numbers_are_jumpers yes)")),
            ["footprint H1 changed"],
        )
        self.assertEqual(
            routing_run.guard(k9, k10.replace('(property "Description" ""', '(property "Description" "Real note"')),
            ["footprint H1 changed"],
        )
        self.assertEqual(
            routing_run.guard(k9, k10.replace('(property "Description" "" (at 0 0) (layer "F.Fab") (hide yes)',
                                              '(property "Description" "" (at 0 0) (layer "F.SilkS")')),
            ["footprint H1 changed"],
        )
        self.assertEqual(
            routing_run.guard(k9, k10.replace("(end 3 0)", "(end 3.1 0)")),
            ["footprint H1 changed"],
        )
        self.assertEqual(
            routing_run.guard(k9, k10.replace('gr_text "3.0"', 'gr_text "3"')),
            ["text '3' added", "text '3.0' removed"],
        )
        self.assertEqual(
            routing_run.guard(k9, k10.replace("(attr exclude_from_pos_files exclude_from_bom)", "(attr exclude_from_pos_files)")),
            ["footprint H1 changed"],
        )
        with_other_fp = k10.replace(
            '(property "Description" ""',
            '(property "Footprint" "other:H"'
            ' (at 0 0) (layer "F.Fab") (hide yes) (effects (font (size 1 1))))'
            ' (property "Description" ""',
        )
        self.assertEqual(routing_run.guard(k9, with_other_fp), ["footprint H1 changed"])


class ProviderDefaultHintTests(unittest.TestCase):
    def test_help_and_missing_provider_refusal_mention_agy_for_gemini(self):
        import subprocess
        help_run = subprocess.run([sys.executable, str(pathlib.Path(__file__).with_name("routing_run.py")), "--help"],
                                  capture_output=True, text=True, check=False)
        self.assertEqual(help_run.returncode, 0, help_run.stderr)
        self.assertIn("gemini runs agy", help_run.stdout)
        text = pathlib.Path(__file__).with_name("routing_run.py").read_text(encoding="utf-8")
        self.assertIn("(--provider gemini uses agy)", text)


if __name__ == "__main__":
    unittest.main()
