#!/usr/bin/env python3
"""editor_step.py's pass rule for the design check, what reviews leave open, its counts of tokens and calls, and how it
knows its editor, with no KiCad: runs on Windows and the Mac."""
from __future__ import annotations

import io
import json
import pathlib
import sys
import tempfile
import unittest
import zipfile
from contextlib import ExitStack
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import editor_step  # noqa: E402


def check(open_, errors, findings):
    return {"open": open_, "errors": errors, "findings": findings}


START = check(19, 19, 46)


class CheckPasses(unittest.TestCase):
    def test_a_closed_connection_passes(self):
        self.assertTrue(editor_step.check_passes(START, check(18, 19, 46)))

    def test_a_closed_connection_fails_with_new_non_isolated_warnings(self):
        self.assertFalse(editor_step.check_passes(START, check(18, 19, 48)))
        with_iso = {**check(18, 19, 48), "isolated_copper": 2}
        self.assertTrue(editor_step.check_passes(START, with_iso, pours_changed=True))
        self.assertFalse(editor_step.check_passes(START, with_iso, pours_changed=False))

    def test_a_closed_connection_with_a_new_error_fails(self):
        self.assertFalse(editor_step.check_passes(START, check(18, 20, 47)))

    def test_one_finding_cleared_and_nothing_else_passes(self):
        self.assertTrue(editor_step.check_passes(START, check(19, 19, 45)))

    def test_an_error_cleared_passes(self):
        self.assertTrue(editor_step.check_passes(START, check(19, 18, 45)))

    def test_nothing_changed_fails(self):
        self.assertFalse(editor_step.check_passes(START, START))

    def test_a_finding_cleared_but_a_connection_opened_fails(self):
        self.assertFalse(editor_step.check_passes(START, check(20, 19, 45)))

    def test_a_finding_cleared_but_an_error_added_fails(self):
        self.assertFalse(editor_step.check_passes(START, check(19, 20, 44)))

    def test_more_findings_and_nothing_closed_fails(self):
        self.assertFalse(editor_step.check_passes(START, check(19, 19, 47)))

    def test_pours_changed_passes_when_no_new_errors(self):
        self.assertTrue(editor_step.check_passes(START, START, pours_changed=True))
        self.assertFalse(editor_step.check_passes(START, check(18, 19, 49), pours_changed=True))
        self.assertTrue(editor_step.check_passes(START, {**check(18, 19, 49), "isolated_copper": 3}, pours_changed=True))
        self.assertFalse(editor_step.check_passes(START, check(19, 20, 46), pours_changed=True))
        self.assertFalse(editor_step.check_passes(START, check(19, 19, 49), pours_changed=True))


def used_bash(call_id, command):
    return json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": call_id, "name": "Bash", "input": {"command": command}}]}})


def answered(call_id, text):
    return json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": call_id, "content": text}]}})


ROUTE = ".venv-kicad-ipc/bin/python scripts/kicad_route.py R17.2 LED2.1 --net /LED2_K --layer F.Cu"
EDIT = ".venv-kicad-ipc/bin/python scripts/kicad_edit.py"
TRANSCRIPT = [
    '{"type":"system","subtype":"init"}',
    used_bash("a", ROUTE), answered("a", 'Exit code 1\n{"ok": false, "closed": false, "status": "pad_entry"}'),
    used_bash("b", ROUTE), answered("b", 'Exit code 1\n{"ok": false, "closed": false, "status": "pad_entry"}'),
    used_bash("c", ROUTE + " --through 111.2,113.45"), answered("c", 'Exit code 1\n{"status": "route_quality"}'),
    used_bash("d", ".venv-kicad-ipc/bin/python scripts/kicad_route.py --help"), answered("d", "usage: kicad_route.py [-h] ..."),
    used_bash("e", f"{EDIT} list --net /LED2_K"), answered("e", '{"ok": true, "verb": "list", "status": "done"}'),
    used_bash("f", f"{EDIT} delete 593be3cf"), answered("f", '{"ok": true, "verb": "delete", "status": "done"}'),
    used_bash("g", ROUTE + " --through 111.9,112.5"), answered("g", '{"ok": true, "closed": true, "status": "closed"}'),
    used_bash("h", ".venv-kicad-ipc/bin/python scripts/mac.py look"), answered("h", "/tmp/look.png"),
    used_bash("i", "date"), answered("i", "Tue Nov 10 18:00:52 MST 2026"),
    used_bash("j", "date"), answered("j", "Tue Nov 10 18:01:52 MST 2026"),
    json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "k", "name": "Read", "input": {"file_path": "/tmp/x.png"}}]}}),
    used_bash("l", ROUTE + " --mode shove"),
    '{"type":"result","subtype":"success"}',
]


class Calls(unittest.TestCase):
    def setUp(self):
        self.found = editor_step.calls(TRANSCRIPT)

    def test_route_calls_by_status(self):
        self.assertEqual(self.found["route_by_status"], {"closed": 1, "no_result": 1, "pad_entry": 2, "route_quality": 1, "usage": 1})
        self.assertEqual((self.found["route"], self.found["route_closed"], self.found["route_refused"]), (6, 1, 3))

    def test_edit_calls_by_verb_looks_and_others(self):
        self.assertEqual(self.found["edit_by_verb"], {"delete": 1, "list": 1})
        self.assertEqual((self.found["edit"], self.found["look"], self.found["other"]), (2, 1, 2))

    def test_repeats_are_word_for_word(self):
        self.assertEqual(self.found["repeats"], {ROUTE: 2, "date": 2})

    def test_an_empty_transcript_counts_nothing(self):
        found = editor_step.calls([])
        self.assertEqual((found["route"], found["edit"], found["look"], found["other"], found["repeats"]), (0, 0, 0, 0, {}))


class TokensTotal(unittest.TestCase):
    def test_the_agent_and_every_review_count(self):
        one = {"input": 2, "output": 10, "thinking": 4, "cache_read": 100, "cache_write": 20}
        used = {"agent": {"rounds": [one, dict(one, thinking=None)], "by_model": {}},
                "reviews": [{"rounds": [one], "by_model": {}}]}
        self.assertEqual(editor_step.tokens_total(used),
                         {"input": 6, "output": 30, "thinking": 8, "cache_read": 300, "cache_write": 60})

    def test_the_run_adds_up_its_steps(self):
        step = {"tokens_total": dict.fromkeys(editor_step.TOKEN_KINDS, 1), "calls": editor_step.calls(TRANSCRIPT)}
        found = editor_step.run_totals([step, step])
        self.assertEqual((found["route_calls"], found["route_closed"], found["route_refused"]), (12, 2, 6))
        self.assertEqual(found["tokens_total"], dict.fromkeys(editor_step.TOKEN_KINDS, 2))

    def test_a_stand_in_says_so(self):
        found = editor_step.run_totals([{"tokens_total": editor_step.NO_AGENT, "calls": editor_step.NO_AGENT}])
        self.assertEqual(set(found.values()), {editor_step.NO_AGENT})


PASS, CHECK_FAILED = {"passed": True, "why": "editor_work"}, {"passed": False, "why": "check_failed"}
SAME = "same"  # a round whose saved board is the one the last review saw


def walk(rounds, answers):
    """A step's rounds as step() runs them with reviews on: each round's result (SAME: a pass on the board the last
    review saw), and each review's answer in turn. The moves, the reviews run, and review_open at the end."""
    answers, moves, reviews, review_open, sent_back = list(answers), [], 0, [], 0
    for result in rounds:
        unchanged = result == SAME
        result = PASS if unchanged else result
        move = editor_step.next_move(result, True, None, sent_back, unchanged)
        if move == "review":
            review_open, reviews = editor_step.still_open(review_open, answers.pop(0)), reviews + 1
            move = editor_step.next_move(result, True, review_open, sent_back)
        moves.append(move)
        if move != "back":
            break
        sent_back += result["passed"]
    return moves, reviews, review_open


HOLE = "MUST FIX: the /D28_ADC11 leg at x 210.1 runs 0.3 mm from mounting hole MH3's edge. Move it east to x 210.9."


class NextMove(unittest.TestCase):
    def test_two_send_backs_then_a_third_review_has_the_last_word(self):
        self.assertEqual(walk([PASS, PASS, PASS], ["MUST FIX: a", "FIXED 1: done\nMUST FIX: b\nMUST FIX: c", "FIXED 1: ok\nFIXED 2: ok\nMUST FIX: d"]),
                         (["back", "back", "stop"], 3, ["MUST FIX: d"]))

    def test_a_third_review_that_calls_everything_fixed_leaves_nothing_open(self):
        self.assertEqual(walk([PASS, PASS, PASS], ["MUST FIX: a", "FIXED 1: gone\nMUST FIX: b", "FIXED 1: gone\nNOTHING"]),
                         (["back", "back", "stop"], 3, []))

    def test_a_failed_round_after_a_send_back_keeps_the_findings(self):
        self.assertEqual(walk([PASS, CHECK_FAILED], ["MUST FIX: a"])[1:], (1, ["MUST FIX: a"]))
        self.assertEqual(editor_step.next_move(CHECK_FAILED, True, None, 1), "back")

    def test_a_script_failure_is_sent_back_and_does_not_count_against_the_reviews(self):
        self.assertEqual(walk([CHECK_FAILED, PASS, PASS, PASS], ["MUST FIX: a", "FIXED 1: yes\nMUST FIX: b", "FIXED 1: yes\nMUST FIX: c"]),
                         (["back", "back", "back", "stop"], 3, ["MUST FIX: c"]))

    def test_an_unchanged_board_after_a_send_back_gets_no_review_and_is_not_kept(self):
        self.assertEqual(walk([PASS, SAME], [HOLE, "FINE AS IS: nothing near the hole"]), (["back", "stop"], 1, [HOLE]))

    def test_a_real_change_that_the_next_review_calls_fixed_is_kept(self):
        self.assertEqual(walk([PASS, PASS], [HOLE, "FIXED 1: the leg is at x 211.0, 2.5 mm from MH3's centre\nFINE AS IS: the detour"]),
                         (["back", "stop"], 2, []))

    def test_a_review_silent_about_an_earlier_finding_leaves_it_open(self):
        self.assertEqual(walk([PASS, PASS, PASS], [HOLE] + ["FINE AS IS: the detour\nFINE AS IS: the diagonal"] * 2),
                         (["back", "back", "stop"], 3, [HOLE]))

    def test_a_new_finding_in_a_later_review_is_open_too(self):
        self.assertEqual(walk([PASS, PASS], ["MUST FIX: a", "STILL OPEN 1: the same\nMUST FIX: b"])[2], ["MUST FIX: a", "MUST FIX: b"])

    def test_still_open_counts_fixed_only_by_number(self):
        earlier = ["MUST FIX: a", "MUST FIX: b", "MUST FIX: c"]
        answer = "- **FIXED 2**: moved\nNOT FIXED 1: no\nSTILL OPEN 3: same\nMUST FIX: a\nThe hole is FIXED 3 says nothing."
        self.assertEqual(editor_step.still_open(earlier, answer), ["MUST FIX: a", "MUST FIX: c"])

    def test_a_review_with_no_answer_is_not_a_pass(self):
        self.assertEqual(editor_step.still_open([], ""), [editor_step.NO_ANSWER])
        self.assertEqual(editor_step.still_open(["MUST FIX: a"], "  \n"), ["MUST FIX: a", editor_step.NO_ANSWER])
        self.assertEqual(editor_step.still_open([editor_step.NO_ANSWER], ""), [editor_step.NO_ANSWER])
        self.assertEqual(editor_step.still_open([], "NOTHING."), [])

    def test_other_failures_stop(self):
        self.assertEqual(editor_step.next_move({"passed": False, "why": "timeout"}, True, None, 0), "stop")

    def test_with_reviews_off_a_pass_stops(self):
        self.assertEqual(editor_step.next_move(PASS, False, None, 0), "stop")


class StopAfter(unittest.TestCase):
    NOT_KEPT = {"kept": False, "passed": False, "why": "check_failed", "review_skipped": ""}

    def test_a_step_not_kept_stops_the_run(self):
        self.assertEqual(editor_step.stop_after("2-undo", self.NOT_KEPT, False), "not kept: 2-undo, check_failed")

    def test_a_passed_step_with_a_review_open_says_so(self):
        found = editor_step.stop_after("1-route", dict(self.NOT_KEPT, passed=True, why="editor_work"), False)
        self.assertEqual(found, "not kept: 1-route, editor_work, review left MUST FIX open")

    def test_keep_going_goes_on_after_a_step_not_kept(self):
        self.assertEqual(editor_step.stop_after("2-undo", self.NOT_KEPT, True), "")

    def test_a_kept_step_goes_on(self):
        self.assertEqual(editor_step.stop_after("1-route", dict(self.NOT_KEPT, kept=True, passed=True), False), "")


class CodexStep(unittest.TestCase):
    def setUp(self):
        self.resources = ExitStack()
        self.addCleanup(self.resources.close)
        self.folder = pathlib.Path(self.resources.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.path = self.folder / "motor.kicad_pcb"
        self.path.write_text("(kicad_pcb)")
        self.args = SimpleNamespace(provider="codex", model=None, review_model=None, effort=None,
                                    agent=None, keep_going=False, once=True, minutes=4)
        self.operations = {"route": 1, "route_closed": 1, "look": 1, "mac": 0, "looked_after_edit": True}
        self.events = []
        self.answer = "NOTHING."
        self.operator_ok = True
        self.reviewer_ok = True
        self.complete = True
        self.missing_picture = False
        self.codex = SimpleNamespace(preflight=mock.Mock(return_value="/opt/codex"), run=self.invoke,
                                     tokens=self.usage, calls=lambda log, **kw: {**editor_step.calls([]), **self.operations})
        self.resources.enter_context(mock.patch.dict(sys.modules, {"routing_codex": self.codex}))
        editor_step.configure_provider(self.args)
        self.resources.enter_context(mock.patch.object(editor_step, "layer_pictures", side_effect=self.capture))
        self.resources.enter_context(mock.patch.object(editor_step, "lock"))
        self.decide = self.resources.enter_context(mock.patch.object(
            editor_step, "decide", return_value=({"passed": True, "why": "editor_work", "before": START,
                                                  "after": check(18, 19, 45)}, [])))
        self.resources.enter_context(mock.patch.object(editor_step, "ROUTING", {"docs": ["truth.md"]}))
        (self.folder / "truth.md").write_text("The owner's review instructions.")

    def usage(self, log, model):
        return {"rounds": [dict.fromkeys(editor_step.TOKEN_KINDS, 1)] if log.is_file() else [],
                "by_model": {model: {}}, "complete": self.complete and log.is_file()}

    def capture(self, socket, pid, folder, tag, board_name):
        self.assertEqual(board_name, "motor")
        files = [folder / f"{tag}-{layer}.png" for layer in ["top", "bottom"]]
        for file in files:
            if self.missing_picture and file.name == "round1-bottom.png":
                continue
            file.write_bytes(b"\x89PNG\r\n\x1a\n")
        return files

    def invoke(self, binary, prompt, model, effort, repo, env, log, seconds, *, pid=None, images=()):
        self.events.append((pid, images, model, effort, prompt))
        log.write_text("fixture")
        if pid is None:
            return self.reviewer_ok, self.answer
        return self.operator_ok, "operator finished" if self.operator_ok else "connection lost"

    def run_step(self):
        proc = SimpleNamespace(pid=975)
        return editor_step.step(self.args, "Connect the spring contact.", self.path, proc,
                                "test socket", START, [], self.folder, self.folder)[0]

    def test_once_still_reviews_both_faces_before_and_after_with_no_editor_pid(self):
        result = self.run_step()
        self.assertTrue(result["passed"])
        self.assertEqual(result["review_open"], [])
        self.assertEqual(result["provider"], result["review_provider"])
        self.assertEqual(result["rounds"], 1)
        self.assertEqual(result["reviews"], 1)
        operator, reviewer = self.events
        self.assertEqual((operator[0], [p.name for p in operator[1]], operator[2], operator[3]),
                         (975, ["start-top.png", "start-bottom.png"], "gpt-6.1-terra", "low"))
        self.assertIsNone(reviewer[0])
        self.assertEqual([p.name for p in reviewer[1]], ["start-top.png", "start-bottom.png", "round1-top.png", "round1-bottom.png"])
        self.assertIn("The owner's review instructions.", reviewer[4])
        self.assertEqual(result["tokens_total"]["output"], 2)

    def test_named_pads_must_be_newly_connected_on_the_saved_board(self):
        task = "Connection: J1.1 J1.2 /MOTOR_A\nJoin the socket pads."
        self.decide.side_effect = lambda *args: ({"passed": True, "why": "editor_work", "after": START.copy()}, [])
        for before, after, kept in [(False, False, False), (True, True, False), (False, True, True)]:
            with self.subTest(before=before, after=after), mock.patch.object(
                    editor_step, "saved_connection", side_effect=[before, after]):
                result, _ = editor_step.step(self.args, task, self.path, SimpleNamespace(pid=975),
                                             "socket", START, [], self.folder, self.folder)
                self.assertEqual(result["passed"], kept)
                if kept:
                    self.assertTrue(result["connection"]["after"])
                    self.assertEqual(result["connection"]["saved_sha256"], editor_step.sha(self.path))

    def test_child_error_prevents_save_and_review(self):
        self.operator_ok = False
        result = self.run_step()
        self.assertFalse(result["passed"])
        self.assertEqual((result["why"], result["detail"]), ("operator_failed", "connection lost"))
        self.decide.assert_not_called()

    def test_route_quality_failure_keeps_the_connection_and_exact_finding_in_the_receipt(self):
        finding = {"rule": "micro_segment", "severity": "warning", "net": "/A", "layer": 0,
                   "point_mm": [2, 3], "message": "0.1250 mm: (2, 3) -> (2.125, 3)"}
        (self.folder / "quality-round1.json").write_text(json.dumps({"findings": [finding]}))
        labels = ["route quality: warning micro_segment on /A"]
        details = editor_step.finding_details(self.folder, "round1", labels)
        self.decide.return_value = ({"passed": False, "why": "audit_failed", "after": START,
                                     "new_findings": labels, "matching_geometry": details}, [])
        (self.folder / "connection-round1.json").write_text('{"connected":true}')
        (self.folder / "drc-end.json").write_text('{"violations":[]}')
        self.args.once = False
        self.answer = "FINE AS IS: Warning 1: Both caps cover the short transition."
        with mock.patch.object(editor_step, "saved_connection", side_effect=[False, True]):
            result, _ = editor_step.step(self.args, "Connection: J1.1 J1.2 /A", self.path,
                                         SimpleNamespace(pid=975), "socket", START, [], self.folder, self.folder)
        first = result["first_check_failure"]
        self.assertTrue(first["connection"]["after"])
        self.assertIn("0.1250 mm", first["matching_geometry"][0])
        self.assertTrue(result["passed"])
        self.assertEqual(result["why"], "warning_accepted")
        self.assertEqual((result["rounds"], result["reviews"]), (1, 1))
        self.assertEqual(result["candidate"]["board_sha256"], editor_step.sha(self.path))
        self.assertEqual(result["reviewed_sha256"], result["candidate"]["board_sha256"])
        self.assertEqual(result["warning_reviews"][0]["answer"], self.answer)
        self.assertIn("0.1250 mm", self.events[1][4])
        self.assertIn(self.path.read_text(), self.events[1][4])
        self.assertIn(result["reviewed_sha256"], self.events[1][4])
        with zipfile.ZipFile(self.folder / result["candidate"]["archive"]) as bundle:
            self.assertEqual(bundle.read("candidate.kicad_pcb"), self.path.read_bytes())
            self.assertIn("round1-candidate-top.png", bundle.namelist())

    def test_each_warning_needs_a_numbered_reason_or_the_route_stays_open(self):
        warnings = ["short segment A", "short segment B"]
        first = "FINE AS IS: Warning 1: Under the wider end cap."
        for answer in ["NOTHING.", first, first + "\n" + first, first + "\nFINE AS IS: Warning 2:  ",
                       first + "\nFINE AS IS: Warning 3: Inside the pad."]:
            with self.subTest(answer=answer), self.assertRaises(ValueError):
                editor_step.review_findings([], answer, warnings=warnings)
        fix = "MUST FIX: Warning 2: Remove the exposed kink."
        self.assertEqual(editor_step.review_findings([], first + "\n" + fix, warnings=warnings), [fix])
        self.assertEqual(editor_step.review_findings([], first + "\nFINE AS IS: Warning 2: Inside the pad.",
                                                    warnings=warnings), [])

    def test_warning_must_fix_returns_to_operator_and_reviews_the_corrected_pcb(self):
        self.args.once = False
        label = "route quality: warning micro_segment on /A"
        fix = "MUST FIX: Warning 1: Straighten the kink before the socket."
        self.answer = fix
        def decide(path, start, audits, proc, socket, watched, finished, folder, tag):
            path.write_text(f"(kicad_pcb (gr_text {tag}))")
            if tag == "round2":
                self.answer = "FIXED 1: The trace enters straight.\nNOTHING."
                return {"passed": True, "why": "editor_work", "after": START}, []
            for name in ("quality-round1.json", "drc-end.json", "connection-round1.json"):
                (folder / name).write_text("{}")
            return {"passed": False, "why": "audit_failed", "after": START,
                    "new_findings": [label], "matching_geometry": [label + ", layer 0 near the socket"]}, [label]
        self.decide.side_effect = decide
        with mock.patch.object(editor_step, "saved_connection", side_effect=[False, True, True]):
            result, _ = editor_step.step(self.args, "Connection: J1.1 J1.2 /A", self.path,
                                         SimpleNamespace(pid=975), "socket", START, [], self.folder, self.folder)
        self.assertEqual([event[0] for event in self.events], [975, None, 975, None])
        self.assertIn(fix, self.events[2][4])
        self.assertEqual(result["warning_reviews"][0]["open_findings"], [fix])
        self.assertTrue(result["passed"])
        self.assertEqual(result["review_open"], [])
        self.assertEqual(result["reviewed_sha256"], editor_step.sha(self.path))
        self.assertNotEqual(result["warning_reviews"][0]["board_sha256"], result["reviewed_sha256"])
        self.assertTrue((self.folder / result["warning_reviews"][0]["candidate"]["archive"]).is_file())

    def test_candidate_archive_contains_each_closeup_named_in_its_saved_report(self):
        report = self.folder / "connection-round2.json"
        names = ["connection-round2-pad1.png", "connection-round2-pad2.png"]
        report.write_text(json.dumps({"pad_exits": [{"picture": name} for name in names]}))
        for name in names:
            (self.folder / name).write_bytes(b"\x89PNG\r\n\x1a\n" + name.encode())
        for name in ("quality-round2.json", "drc-end.json", "connection-round1-pad1.png"):
            (self.folder / name).write_text("stale or other evidence")
        connection = {"saved_sha256": editor_step.sha(self.path)}
        editor_step.save_candidate(self.path, self.folder, "round2", "socket", 1, connection)
        with zipfile.ZipFile(self.folder / "candidate-round2.zip") as archive:
            self.assertNotIn("connection-round1-pad1.png", archive.namelist())
            for entry in json.loads(archive.read(report.name))["pad_exits"]:
                self.assertEqual(archive.read(entry["picture"]), (self.folder / entry["picture"]).read_bytes())

        (self.folder / names[1]).unlink()
        with self.assertRaises(FileNotFoundError):
            editor_step.save_candidate(self.path, self.folder, "round2", "socket", 1, connection)
        report.write_text(json.dumps({"pad_exits": [{"picture": "../outside.png"}]}))
        with self.assertRaisesRegex(ValueError, "Expected connection-round2-pad1.png"):
            editor_step.save_candidate(self.path, self.folder, "round2", "socket", 1, connection)

    def test_handoff_preserves_grace_for_operator_and_separate_review_time(self):
        with mock.patch.object(self.codex, "run", wraps=self.invoke) as run:
            result = self.run_step()
        self.assertTrue(result["passed"])
        operator, reviewer = run.call_args_list
        self.assertGreater(operator.args[7], self.args.minutes * 60)
        self.assertEqual(reviewer.args[7], self.args.minutes * 60)
        self.assertIn("ROUTING_HANDOFF_AT", operator.args[5])

    def test_successful_child_without_editor_evidence_is_rejected(self):
        self.operations = {"route": 0, "edit": 0, "look": 3, "mac": 0}
        result = self.run_step()
        self.assertFalse(result["passed"])
        self.assertEqual(result["why"], "missing_editor_evidence")
        self.assertEqual(len(self.events), 1)

    def test_operator_prompt_names_kicad_route_edit_and_idle_tools(self):
        result = self.run_step()
        self.assertTrue(result["passed"])
        prompt = self.events[0][4]
        for tool in ("scripts/kicad_route.py", "--width", "scripts/kicad_edit.py", "scripts/kicad_ipc_status.py", "view_image"):
            self.assertIn(tool, prompt)

    def test_operator_without_visual_look_after_edit_is_sent_back_and_recovers(self):
        self.args.once = False
        self.decide.side_effect = lambda *args: (
            {"passed": True, "why": "editor_work", "before": START, "after": check(18, 19, 45)}, [])
        calls_by_round = [
            {"route": 1, "route_closed": 1, "look": 0, "mac": 0, "looked_after_edit": False},
            {"route": 1, "route_closed": 1, "look": 1, "mac": 0, "looked_after_edit": True},
        ]
        self.codex.calls = lambda log, **kw: {**editor_step.calls([]), **(calls_by_round.pop(0) if calls_by_round else {"route": 1, "look": 1, "looked_after_edit": True})}
        result = self.run_step()
        self.assertTrue(result["passed"])
        self.assertEqual(result["rounds"], 2)
        self.assertEqual(result["reviews"], 1)
        self.assertFalse(result["stalled"])
        self.assertIn("mac.py look", self.events[1][4])
        self.assertIn("view_image", self.events[1][4])

    def test_operator_that_never_looks_is_rejected_without_review(self):
        self.args.once = False
        self.decide.side_effect = lambda *args: (
            {"passed": True, "why": "editor_work", "before": START, "after": check(18, 19, 45)}, [])
        self.operations = {"route": 1, "route_closed": 1, "look": 0, "mac": 0, "looked_after_edit": False}
        result = self.run_step()
        self.assertFalse(result["passed"])
        self.assertEqual(result["why"], "missing_visual_look")
        self.assertEqual(result["rounds"], 2)
        self.assertEqual(result["reviews"], 0)

    def test_ad_hoc_wrapper_is_rejected_before_review(self):
        self.operations = {"route": 0, "look": 2, "mac": 5, "looked_after_edit": True, "wrapper_calls": 1}
        result = self.run_step()
        self.assertFalse(result["passed"])
        self.assertEqual(result["why"], "unauthorized_wrapper")
        self.assertEqual(result["reviews"], 0)

    def test_editor_busy_sends_operator_back_without_stalling_and_recovers(self):
        self.args.once = False
        rounds = [
            ({"passed": False, "why": "editor_busy",
              "detail": "KiCad returned error: KiCad is busy and cannot respond to API requests right now"}, None),
            ({"passed": True, "why": "editor_work", "before": START, "after": check(18, 19, 45)}, []),
        ]
        self.decide.side_effect = lambda *args: rounds.pop(0)
        result = self.run_step()
        self.assertTrue(result["passed"])
        self.assertEqual(result["rounds"], 2)
        self.assertEqual(result["reviews"], 1)
        self.assertFalse(result["stalled"])
        self.assertIn("KiCad reported that it is busy", self.events[1][4])

    def test_persistent_editor_busy_stops_after_retry_cap(self):
        self.args.once = False
        self.decide.side_effect = lambda *args: (
            {"passed": False, "why": "editor_busy", "detail": "KiCad is busy"}, None)
        result = self.run_step()
        self.assertFalse(result["passed"])
        self.assertEqual(result["why"], "editor_busy")
        self.assertEqual(result["rounds"], 3)
        self.assertEqual(result["reviews"], 0)

    def test_missing_image_rejects_route_before_review(self):
        self.missing_picture = True
        result = self.run_step()
        self.assertFalse(result["passed"])
        self.assertIn("round1-bottom.png", result["detail"])
        self.assertEqual(result["reviews"], 0)
        self.assertIsNone(result["review_provider"])
        self.assertEqual(len(self.events), 1)

    def test_failed_review_cannot_clear_even_with_a_passing_text(self):
        self.reviewer_ok = False
        result = self.run_step()
        self.assertFalse(result["passed"])
        self.assertEqual(result["why"], "review_failed")

    def test_normal_entry_restores_the_step_after_reviewer_failure(self):
        self.reviewer_ok = False
        initial = self.path.read_bytes()
        restored = []
        proc = SimpleNamespace(pid=975)

        def save_and_check(path, *args):
            path.write_bytes(initial + b"\n")  # saved by the runner, after the operator finished
            return {"passed": True, "why": "editor_work", "after": check(18, 19, 45)}, []

        def restore(path, copy, proc, socket):
            self.assertNotEqual(path.read_bytes(), copy.read_bytes())
            path.write_bytes(copy.read_bytes())
            restored.append(path)
            return {"file_same_as_copy": True}, proc, socket

        self.decide.side_effect = save_and_check
        self.resources.enter_context(mock.patch.object(editor_step, "ROUTING", {}))
        for name, returned in {"screen_locked": False, "refuse_if_editor_running": None, "open_editor": (proc, "socket"),
                               "not_idle": "", "close_editor": None, "editor_board": None, "design_check": START,
                               "audits": []}.items():
            self.resources.enter_context(mock.patch.object(editor_step, name, return_value=returned))
        self.resources.enter_context(mock.patch.object(editor_step, "put_back", side_effect=restore))
        self.resources.enter_context(mock.patch.object(sys, "argv", ["editor_step.py", str(self.path), "--task", "Route A", "--provider", "codex"]))
        lines = self.resources.enter_context(mock.patch.object(sys, "stdout", new_callable=io.StringIO))
        self.assertEqual(editor_step.main(), 1)
        self.assertEqual(restored, [self.path])
        self.assertEqual(self.path.read_bytes(), initial)
        step_result, summary = map(json.loads, lines.getvalue().splitlines())
        self.assertFalse(step_result["kept"])
        self.assertEqual(summary["kept"], 0)

    def test_malformed_review_cannot_clear_the_step(self):
        self.answer = "I couldn't see those captures."
        result = self.run_step()
        self.assertFalse(result["passed"])
        self.assertEqual(result["why"], "review_failed")

    def test_unmetered_invocation_is_unknown_in_step_and_run(self):
        self.operator_ok, self.complete = False, False
        result = self.run_step()
        self.assertIsNone(result["tokens_total"])
        self.assertIsNone(editor_step.run_totals([result])["tokens_total"])

    def test_codex_does_not_use_a_stand_in_or_skip_failed_steps(self):
        for name in ["agent", "keep_going"]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                editor_step.configure_provider(SimpleNamespace(**{**vars(self.args), name: True}))

    def test_changed_board_during_review_cannot_get_a_reviewed_hash(self):
        def change_board(*args, **kwargs):
            response = self.invoke(*args, **kwargs)
            if kwargs.get("pid") is None:
                self.path.write_text("(kicad_pcb (version 1))")
            return response
        self.codex.run = change_board
        result = self.run_step()
        self.assertFalse(result["passed"])
        self.assertEqual(result["why"], "review_failed")
        self.assertNotIn("reviewed_sha256", result)

    def test_review_must_reference_a_real_finding_once(self):
        self.assertEqual(editor_step.review_findings([HOLE], "FIXED 1: the leg moved.\nNOTHING."), [])
        for answer in ["", "FIXED 2: moved.", "FIXED 1: moved.\nSTILL OPEN 1: same.", "MUST FIX: "]:
            with self.subTest(answer=answer), self.assertRaises(ValueError):
                editor_step.review_findings([HOLE], answer)


    def test_scoped_exit_gate_and_per_pad_review(self):
        from pad_exits import crossing_report
        self.resources.enter_context(mock.patch.object(editor_step.validate_board, "ELECTRICAL", None))
        task = "Connection: SP1.1 J1.1 /MOTOR_A\nPad exits: centered"
        good = crossing_report(.75, 1.05, [((0, 0), (0, 1.8), .5)])
        bad = crossing_report(.75, 1.05, [((0, 0), (.526663, .526663), .5),
                                         ((.526663, .526663), (.526663, 1.8), .5)])
        self.assertAlmostEqual(bad["crossings"][0]["offset_mm"], .526663)
        self.assertEqual(good["issues"], [])
        def run(geometry, extra_findings=()):
            self.events.clear()
            self.decide.side_effect = lambda *a: ({"passed": not extra_findings,
                                                  "why": "audit_failed" if extra_findings else "editor_work",
                                                  "new_findings": list(extra_findings), "after": START.copy()}, [])
            def inspect(path, target, output, centered=False):
                if centered:
                    entries = [dict(item, pad=name, picture=f"close{i}.png")
                               for i, (name, item) in enumerate(zip(target[:2], [geometry, good]))]
                    for entry in entries:
                        (self.folder / entry["picture"]).write_bytes(b"\x89PNG\r\n\x1a\n")
                    output.write_text(json.dumps({"pad_exits": entries}))
                return centered
            with mock.patch.object(editor_step, "saved_connection", side_effect=inspect):
                return editor_step.step(self.args, task, self.path, SimpleNamespace(pid=975),
                                        "socket", START, [], self.folder, self.folder)[0]
        rejected = run(bad)
        self.assertEqual((rejected["why"], rejected["reviews"]), ("pad_exit_failed", 0))
        self.assertFalse(rejected["passed"])
        self.assertIn("SP1.1: edge offset 0.5267", rejected["detail"])
        mixed = run(bad, ["route quality: warning micro_segment on /MOTOR_A"])
        self.assertEqual(mixed["why"], "pad_exit_failed")
        self.assertIn("micro_segment", mixed["detail"])
        self.assertEqual(editor_step.next_move(mixed, True, None, 0), "back")
        self.assertNotIn("candidate", mixed)
        silent = run(good)
        self.assertEqual(silent["why"], "review_failed")
        self.answer = "PAD SP1.1: MUST FIX: kink under the contact.\nPAD J1.1: centered on the socket.\nNOTHING."
        defect = run(good)
        self.assertEqual(defect["review_open"], ["MUST FIX: SP1.1: kink under the contact."])
        self.assertEqual(defect["reviews"], 1)
        self.answer = self.answer.replace("MUST FIX: kink under the contact.", "exits straight through the middle.")
        accepted = run(good)
        self.assertTrue(accepted["passed"])
        self.assertEqual(accepted["reviewed_sha256"], accepted["connection"]["saved_sha256"])
        self.assertIn("put bends in open space", self.events[0][4])
        self.assertEqual(len(self.events[1][1]), 6)
        self.assertIn("0.0000 mm", self.events[1][4])
        self.assertIn("no separate electrical audit configured", self.events[1][4])

    def test_multi_connection_chunk_deduplicates_pads_retries_partial_and_runs_one_cold_review(self):
        from pad_exits import crossing_report
        self.resources.enter_context(mock.patch.object(editor_step.validate_board, "ELECTRICAL", None))
        task = "Connection: SP1.1 J1.1 /M1_A\nConnection: SP1.1 J1.2 /M1_A\nPad exits: centered\nRoute the chunk."
        good = crossing_report(.75, 1.05, [((0, 0), (0, 1.8), .5)])
        bad = crossing_report(.75, 1.05, [((0, 0), (.5, .5), .5), ((.5, .5), (.5, 1.8), .5)])

        # Refuses before starting if the 2nd connection is already closed.
        with mock.patch.object(editor_step, "saved_connection", side_effect=[False, True]):
            refused, _ = editor_step.step(self.args, task, self.path, SimpleNamespace(pid=975),
                                          "socket", START, [], self.folder, self.folder)
        self.assertFalse(refused["passed"])
        self.assertIn("SP1.1 J1.2 /M1_A", refused["detail"])
        self.assertEqual(len(self.events), 0)

        # Partial close on round 1 sends operator back; round 2 closes both and runs one cold review.
        self.args.once = False
        self.events.clear()
        round_idx = [0]
        def fake_decide(path, *a):
            round_idx[0] += 1
            path.write_text(f'(kicad_pcb (segment (start 0 0) (end {round_idx[0]} 0) (width 0.2) (layer "F.Cu") (net "/M1_A")))')
            return {"passed": True, "why": "editor_work", "before": START, "after": check(17, 19, 45)}, []
        self.decide.side_effect = fake_decide

        seq = [
            False, False,  # pre-start checks for conn 1, conn 2
            True, False,   # round 1: conn 1 closed, conn 2 still open -> connection_open send-back
            True, True,    # round 2: both closed
        ]
        def inspect(path, target, output, centered=False):
            joined = seq.pop(0)
            if centered:
                entries = [dict(good, pad=name, picture=f"{output.stem}-pad{i}.png") for i, name in enumerate(target[:2], 1)]
                for entry in entries:
                    (self.folder / entry["picture"]).write_bytes(b"\x89PNG\r\n\x1a\n")
                output.write_text(json.dumps({"pad_exits": entries}))
            return joined

        self.answer = ("PAD SP1.1: straight.\nPAD J1.1: straight.\nPAD J1.2: straight.\nNOTHING.")
        with mock.patch.object(editor_step, "saved_connection", side_effect=inspect):
            result, _ = editor_step.step(self.args, task, self.path, SimpleNamespace(pid=975),
                                         "socket", START, [], self.folder, self.folder)
        self.assertTrue(result["passed"])
        self.assertEqual((result["rounds"], result["reviews"], result["review_open"]), (2, 1, []))
        self.assertNotIn("connection", result)
        self.assertEqual([[c["from"], c["to"], c["net"]] for c in result["connections"]],
                         [["SP1.1", "J1.1", "/M1_A"], ["SP1.1", "J1.2", "/M1_A"]])
        self.assertEqual([p["pad"] for p in result["pad_exits"]], ["SP1.1", "J1.1", "J1.2"])
        self.assertEqual([e[0] for e in self.events], [975, 975, None])
        self.assertIn("Route the remaining open connection(s)", self.events[1][4])
        self.assertEqual(len(self.events[2][1]), 7)  # 4 board views + 3 deduplicated pad closeups

        # Shared pad issue on second report fails pad_exit_failed even when first report was clean.
        self.args.once = True
        self.events.clear()
        self.path.write_text("(kicad_pcb)")
        def inspect_shared_bad(path, target, output, centered=False):
            if not centered:
                return False
            first_item = bad if target[1] == "J1.2" else good
            entries = [dict(item, pad=name, picture=f"{output.stem}-pad{i}.png")
                       for i, (name, item) in enumerate(zip(target[:2], [first_item, good]), 1)]
            for entry in entries:
                (self.folder / entry["picture"]).write_bytes(b"\x89PNG\r\n\x1a\n")
            output.write_text(json.dumps({"pad_exits": entries}))
            return True
        with mock.patch.object(editor_step, "saved_connection", side_effect=inspect_shared_bad):
            bad_res, _ = editor_step.step(self.args, task, self.path, SimpleNamespace(pid=975),
                                          "socket", START, [], self.folder, self.folder)
        self.assertFalse(bad_res["passed"])
        self.assertEqual(bad_res["why"], "pad_exit_failed")
        self.assertIn("SP1.1:", bad_res["detail"])

    def test_multi_connection_warning_candidate_archives_all_reports_and_clears_review(self):
        from pad_exits import crossing_report
        self.resources.enter_context(mock.patch.object(editor_step.validate_board, "ELECTRICAL", None))
        task = "Connection: SP1.1 J1.1 /A\nConnection: SP1.1 J1.2 /A\nPad exits: centered"
        good = crossing_report(.75, 1.05, [((0, 0), (0, 1.8), .5)])
        finding = {"rule": "micro_segment", "severity": "warning", "net": "/A", "layer": 0,
                   "point_mm": [2, 3], "message": "0.1250 mm: (2, 3) -> (2.125, 3)"}
        (self.folder / "quality-round1.json").write_text(json.dumps({"findings": [finding]}))
        (self.folder / "drc-end.json").write_text('{"violations":[]}')
        labels = ["route quality: warning micro_segment on /A"]
        details = editor_step.finding_details(self.folder, "round1", labels)
        self.decide.return_value = ({"passed": False, "why": "audit_failed", "after": START,
                                     "new_findings": labels, "matching_geometry": details}, [])
        def inspect(path, target, output, centered=False):
            if centered:
                entries = [dict(good, pad=name, picture=f"{output.stem}-pad{i}.png") for i, name in enumerate(target[:2], 1)]
                for entry in entries:
                    (self.folder / entry["picture"]).write_bytes(b"\x89PNG\r\n\x1a\n" + entry["picture"].encode())
                output.write_text(json.dumps({"connected": True, "pad_exits": entries}))
            else:
                output.write_text('{"connected": false}')
            return centered
        self.args.once = False
        self.answer = ("PAD SP1.1: straight.\nPAD J1.1: straight.\nPAD J1.2: straight.\n"
                       "FINE AS IS: Warning 1: Inside the wider end cap.")
        with mock.patch.object(editor_step, "saved_connection", side_effect=inspect):
            result, _ = editor_step.step(self.args, task, self.path, SimpleNamespace(pid=975),
                                         "socket", START, [], self.folder, self.folder)
        self.assertTrue(result["passed"])
        self.assertEqual(result["why"], "warning_accepted")
        self.assertEqual(result["reviewed_sha256"], editor_step.sha(self.path))
        with zipfile.ZipFile(self.folder / result["candidate"]["archive"]) as bundle:
            names = set(bundle.namelist())
        for expected in ("connection-round1-1.json", "connection-round1-2.json",
                         "connection-round1-1-pad1.png", "connection-round1-1-pad2.png",
                         "connection-round1-2-pad1.png", "connection-round1-2-pad2.png"):
            self.assertIn(expected, names)


class PadExitGeometry(unittest.TestCase):
    def test_crossing_must_be_reachable_by_unbroken_trace_centerlines(self):
        from pad_exits import crossing_report
        def report(stub_end, next_start):
            pieces = [((0, 1.8), next_start, .3), ((0, 0), stub_end, .5)]
            return crossing_report(.75, 1.05, pieces)
        for start in [(0, .75), (.01, .25), (0, .250001)]:
            with self.subTest(start=start):
                self.assertTrue(report((0, .25), start)["issues"])
        centered = crossing_report(.75, 1.05, [((.019, 0), (.019, 2), .5)])
        self.assertEqual(centered["issues"], [])
        self.assertTrue(report((0, .001), (0, .01))["issues"])
        for start in [(0, .25), (0, .125)]:
            with self.subTest(start=start):
                self.assertEqual(report((0, .25), start)["issues"], [])

    def test_pad_finding_cannot_be_cleared_by_a_contradictory_verdict(self):
        verdict = "PAD SP1.1: MUST FIX: kink under the contact.\nPAD J1.1: enters straight.\nNOTHING."
        finding = "MUST FIX: SP1.1: kink under the contact."
        self.assertEqual(editor_step.review_findings([], verdict, ["SP1.1", "J1.1"]), [finding])
        self.assertEqual(editor_step.review_findings([finding], "FIXED 1: cleared.\n" + verdict,
                                                    ["SP1.1", "J1.1"]), [finding])

    def test_edge_split_wrong_side_extra_crossing_and_internal_detour(self):
        from pad_exits import crossing_report
        def issues(points):
            return crossing_report(.75, 1.05, [(a, b, .5) for a, b in zip(points, points[1:])])["issues"]
        self.assertEqual(issues([(0, 0), (0, 1.05), (0, 1.6), (.5, 2.1)]), [])
        for points in [[(0, 0), (0, -2)], [(0, 0), (0, 2), (.5, .5)],
                       [(0, 0), (.1, .3), (0, .5), (0, 2)], [(.4, .3), (.4, 2)],
                       [(0, 0), (.4, 2)], [(0, 0), (1, 0)]]:
            with self.subTest(points=points):
                self.assertTrue(issues(points))

    def test_roundrect_and_oval_centered_exits_use_true_perimeter(self):
        import pad_exits
        w, h, r = 0.5, 1.0, 0.25  # standard 0.25 roundrect ratio: flat short edge is [-0.25, 0.25]
        clean = pad_exits.crossing_report(w, h, [((0, 0), (0, 1.8), 0.2)], corner_radius=r)
        self.assertEqual(clean["issues"], [])
        self.assertEqual(len(clean["crossings"]), 1)
        self.assertAlmostEqual(clean["crossings"][0]["point_mm"][1], h)
        # A trace skirting outside the rounded corner (inside bbox, outside copper) must not trigger crossings or bends.
        skirting = pad_exits.crossing_report(
            w, h, [((0, 0), (0, 1.8), 0.2), ((0.46, 1.0), (0.5, 0.96), 0.2)], corner_radius=r
        )
        self.assertEqual(skirting["issues"], [])
        self.assertEqual(len(skirting["crossings"]), 1)
        # Exiting through the rounded corner arc fails with exact arc crossing (y < h) and wrong edge.
        corner = pad_exits.crossing_report(w, h, [((0.4, 0.0), (0.4, 1.8), 0.2)], corner_radius=r)
        self.assertFalse(corner["crossings"][0]["facing_edge"])
        self.assertAlmostEqual(corner["crossings"][0]["point_mm"][1], 0.95)
        self.assertIn("trace crosses a different pad edge", corner["issues"])
        # Full-radius rounded end (ratio == 0.5 / oval) still accepts centered exits within OFFSET_MM.
        oval_ok = pad_exits.crossing_report(w, h, [((0.015, 0), (0.015, 1.8), 0.2)], corner_radius=w)
        self.assertEqual(oval_ok["issues"], [])
        oval_off = pad_exits.crossing_report(w, h, [((0.05, 0), (0.05, 1.8), 0.2)], corner_radius=w)
        self.assertTrue(oval_off["issues"])

    def test_measure_accepts_roundrect_and_oval_and_rejects_circle_or_pth(self):
        import types
        import pad_exits
        class Vec:
            def __init__(self, x, y): self.x, self.y = int(round(x * 1e6)), int(round(y * 1e6))
        class StubTrack:
            def __init__(self, a, b, width=0.2, net="/SIG", layer=0):
                self._a, self._b, self._w, self._net, self._layer = Vec(*a), Vec(*b), int(round(width * 1e6)), net, layer
            def GetNetname(self): return self._net
            def IsOnLayer(self, layer): return layer == self._layer
            def GetStart(self): return self._a
            def GetEnd(self): return self._b
            def GetWidth(self): return self._w
        class StubPad:
            def __init__(self, pos, size, shape, attr=1, angle=0.0, ratio=0.25, layer=0):
                self._pos, self._size, self._shape, self._attr = Vec(*pos), Vec(*size), shape, attr
                self._angle, self._ratio, self._layer = angle, ratio, layer
            def GetLayer(self): return self._layer
            def GetAttribute(self): return self._attr
            def GetShape(self, layer): return self._shape
            def GetRoundRectRadiusRatio(self, layer): return self._ratio
            def ShapePos(self, layer): return self._pos
            def GetSize(self, layer): return self._size
            def GetOrientationDegrees(self): return self._angle
        stub_pcbnew = types.SimpleNamespace(
            PAD_ATTRIB_SMD=1, PAD_ATTRIB_PTH=0,
            PAD_SHAPE_RECTANGLE=1, PAD_SHAPE_ROUNDRECT=2, PAD_SHAPE_OVAL=3, PAD_SHAPE_CIRCLE=4,
            PCB_TRACK=StubTrack, ToMM=lambda v: v / 1e6,
        )
        board = types.SimpleNamespace(GetTracks=lambda: [StubTrack((0.0, 0.0), (0.0, 5.0))])
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(sys.modules, {"pcbnew": stub_pcbnew}):
            out = pathlib.Path(tmp) / "conn.json"
            pads = {
                "J1.1": StubPad((0.0, 0.0), (0.5, 1.5), stub_pcbnew.PAD_SHAPE_ROUNDRECT, ratio=0.25),
                "J2.1": StubPad((0.0, 5.0), (0.5, 1.5), stub_pcbnew.PAD_SHAPE_OVAL),
            }
            reports = pad_exits.measure(board, pads, "/SIG", out)
            self.assertEqual([r["pad"] for r in reports], ["J1.1", "J2.1"])
            self.assertEqual([r["issues"] for r in reports], [[], []])
            self.assertTrue((pathlib.Path(tmp) / "conn-pad1.png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
            class StubBox:
                def __init__(self, touches): self._touches = touches
                def Intersects(self, other): return bool(self._touches and getattr(other, "_touches", False))
            class StubVia:
                def __init__(self, touches, net="/SIG", layer=0): self._touches, self._net, self._layer = touches, net, layer
                def GetNetname(self): return self._net
                def IsOnLayer(self, layer): return layer == self._layer
                def GetBoundingBox(self): return StubBox(self._touches)
            pads["J1.1"].GetBoundingBox = lambda: StubBox(True)
            pads["J2.1"].GetBoundingBox = lambda: StubBox(False)
            via_board = types.SimpleNamespace(GetTracks=lambda: [StubTrack((0.0, 0.0), (0.0, 5.0)), StubVia(True)])
            via_reports = pad_exits.measure(via_board, pads, "/SIG", out)
            self.assertEqual(via_reports[0]["issues"], ["via or curved track touches the pad; move it clear of the pad edge"])
            self.assertEqual(via_reports[1]["issues"], [])
            bad_circle = {"J1.1": StubPad((0.0, 0.0), (0.5, 1.5), stub_pcbnew.PAD_SHAPE_CIRCLE), "J2.1": pads["J2.1"]}
            with self.assertRaisesRegex(ValueError, "rectangular, rounded-rectangle or oval SMD pad"):
                pad_exits.measure(board, bad_circle, "/SIG", out)
            bad_pth = {"J1.1": StubPad((0.0, 0.0), (0.5, 1.5), stub_pcbnew.PAD_SHAPE_RECTANGLE, attr=0), "J2.1": pads["J2.1"]}
            with self.assertRaisesRegex(ValueError, "require an SMD pad"):
                pad_exits.measure(board, bad_pth, "/SIG", out)

    def test_preflight_tasks_catches_all_static_pad_and_task_errors_at_once(self):
        import pad_exits
        pcb = """(kicad_pcb
            (footprint "Conn" (layer "F.Cu") (at 0 0 0)
                (property "Reference" "J1" (at 0 0 0))
                (pad "1" smd roundrect (at 0 0 0) (size 0.5 1.5) (roundrect_rratio 0.25) (layers "F.Cu") (net 1 "/S1"))
                (pad "2" smd circle (at 2 0 0) (size 0.5 0.5) (layers "F.Cu") (net 2 "/S2"))
                (pad "3" smd rect (at 4 0 0) (size 0.5 1.5) (layers "F.Cu") (net 3 "/S3"))
            )
            (footprint "Conn" (layer "F.Cu") (at 0 5 0)
                (property "Reference" "J2" (at 0 0 0))
                (pad "1" smd oval (at 0 0 0) (size 0.5 1.5) (layers "F.Cu") (net 1 "/S1"))
                (pad "2" thru_hole rect (at 2 0 0) (size 0.5 1.5) (layers "*.Cu") (net 2 "/S2"))
                (pad "3" smd rect (at 6 -5 0) (size 0.5 1.5) (layers "F.Cu") (net 3 "/S3"))
            )
        )"""
        tasks = [
            ("01-ok.txt", "Connection: J1.1 J2.1 /S1\nPad exits: centered\n"),
            ("02-freeform.txt", "Route the short jumper cleanly.\n"),
            ("03-bad-shapes.txt", "Connection: J1.2 J2.2 /S2\nPad exits: centered\n"),
            ("04-nonfacing.txt", "Connection: J1.3 J2.3 /S3\nPad exits: centered\n"),
            ("05-missing-and-net.txt", "Connection: J1.1 J9.1 /WRONG\nPad exits: centered\n"),
        ]
        problems = pad_exits.preflight_tasks(pcb, tasks)
        joined = "\n".join(problems)
        self.assertNotIn("01-ok.txt", joined)
        self.assertNotIn("02-freeform.txt", joined)
        self.assertIn("03-bad-shapes.txt: J1.2: centered exits require a rectangular, rounded-rectangle or oval SMD pad (found circle)", joined)
        self.assertIn("03-bad-shapes.txt: J2.2: centered exits require an SMD pad (found thru_hole)", joined)
        self.assertIn("04-nonfacing.txt: J1.3: neither short edge faces the other pad", joined)
        self.assertIn("05-missing-and-net.txt: J1.1 is on '/S1', expected '/WRONG'", joined)
        self.assertIn("05-missing-and-net.txt: J9.1 is not on the board", joined)

    def test_multi_connection_parsing_and_preflight_checks_every_line(self):
        import pad_exits
        import routing_evidence
        pcb = """(kicad_pcb
            (footprint "Conn" (layer "F.Cu") (at 0 0 0)
                (property "Reference" "J1" (at 0 0 0))
                (pad "1" smd rect (at 0 0 0) (size 0.5 1.5) (layers "F.Cu") (net 1 "/S1"))
                (pad "2" smd circle (at 2 0 0) (size 0.5 0.5) (layers "F.Cu") (net 2 "/S2"))
            )
            (footprint "Conn" (layer "F.Cu") (at 0 5 0)
                (property "Reference" "J2" (at 0 0 0))
                (pad "1" smd rect (at 0 0 0) (size 0.5 1.5) (layers "F.Cu") (net 1 "/S1"))
                (pad "2" smd rect (at 2 0 0) (size 0.5 1.5) (layers "F.Cu") (net 2 "/S2"))
                (pad "3" smd rect (at 4 0 0) (size 0.5 1.5) (layers "F.Cu") (net 1 "/S1"))
            )
        )"""
        good_chunk = "Connection: J1.1 J2.1 /S1\nConnection: J1.1 J2.3 /S1\nPad exits: centered\nRoute chunk.\n"
        self.assertEqual(routing_evidence.parse_task_connections(good_chunk), [["J1.1", "J2.1", "/S1"], ["J1.1", "J2.3", "/S1"]])
        bad_second = "Connection: J1.1 J2.1 /S1\nConnection: J1.2 J2.2 /S2\nPad exits: centered\n"
        dup = "Connection: J1.1 J2.1 /S1\nConnection: J2.1 J1.1 /S1\n"
        stray = "Connection: J1.1 J2.1 /S1\nPad exits: centered\nConnection: J1.1 J2.3 /S1\n"
        for bad_text in (dup, stray, "Connection: J1.1 J2.1 /S1\nConnection: bad\n"):
            with self.subTest(bad_text=bad_text), self.assertRaises(ValueError):
                routing_evidence.parse_task_connections(bad_text)
        problems = pad_exits.preflight_tasks(pcb, [("01-ok.txt", good_chunk), ("02-bad-second.txt", bad_second),
                                                   ("03-dup.txt", dup), ("04-stray.txt", stray)])
        joined = "\n".join(problems)
        self.assertNotIn("01-ok.txt", joined)
        self.assertIn("02-bad-second.txt: J1.2:", joined)
        self.assertIn("03-dup.txt: Duplicate Connection:", joined)
        self.assertIn("04-stray.txt: Place all Connection:", joined)

        # Mode header parsing & preflight validation (in either header order).
        self.assertEqual(routing_evidence.effective_task_mode("Repair: fix"), "hybrid")
        self.assertEqual(routing_evidence.parse_task_mode("Connection: J1.1 J2.1 /S1\nMode: ipc\n"), "ipc")
        self.assertEqual(routing_evidence.parse_task_connections("Mode: ipc\nConnection: J1.1 J2.1 /S1\nPad exits: centered\n"),
                         [["J1.1", "J2.1", "/S1"]])
        bad_mode_problems = pad_exits.preflight_tasks(pcb, [
            ("05-ok-mode.txt", "Mode: ipc\nConnection: J1.1 J2.1 /S1\nPad exits: centered\n"),
            ("05b-legacy-autoroute.txt", "Mode: autoroute\nConnection: J1.1 J2.1 /S1\n"),
            ("06-bad-mode.txt", "Mode: magic\nConnection: J1.1 J2.1 /S1\n"),
            ("07-dup-mode.txt", "Mode: ipc\nMode: hybrid\nConnection: J1.1 J2.1 /S1\n"),
        ])
        mode_joined = "\n".join(bad_mode_problems)
        self.assertNotIn("05-ok-mode.txt", mode_joined)
        self.assertIn("05b-legacy-autoroute.txt: Unsupported routing mode", mode_joined)
        self.assertIn("06-bad-mode.txt: Unsupported routing mode", mode_joined)
        self.assertIn("07-dup-mode.txt: Use Mode:", mode_joined)


class WhichBoard(unittest.TestCase):
    def test_a_project_folder_and_a_project_file_name_the_same_board(self):
        self.assertEqual(editor_step.board_path("/a/run/board", "demo.kicad_pcb"),
                         editor_step.board_path("/a/run/board/demo.kicad_pcb".replace(".kicad_pcb", ".kicad_pro"), "demo.kicad_pcb"))

    def test_a_same_named_board_in_another_folder_is_another_board(self):
        ours = pathlib.Path("/a/run/board/demo.kicad_pcb")
        self.assertFalse(editor_step.same_board(editor_step.board_path("/a/other/board", "demo.kicad_pcb"), ours))
        self.assertTrue(editor_step.same_board(editor_step.board_path("/a/run/board", "demo.kicad_pcb"), ours))

    def test_another_file_name_is_another_board(self):
        self.assertFalse(editor_step.same_board(editor_step.board_path("/a/run/board", "other.kicad_pcb"),
                                                pathlib.Path("/a/run/board/demo.kicad_pcb")))


class RunningEditors(unittest.TestCase):
    def test_the_editor_program_counts(self):
        lines = ("40210 /Applications/KiCad/KiCad.app/Contents/Applications/pcbnew.app/Contents/MacOS/pcbnew /Users/x/run1/board/demo.kicad_pcb\n"
                 "40300 pcbnew\n")
        self.assertEqual(editor_step.running_editors(lines), [40210, 40300])

    def test_a_command_that_only_names_the_editor_does_not(self):
        lines = ("512 /bin/sh -c pkill -f pcbnew\n"
                 "513 /usr/bin/python3 scripts/editor_step.py /tmp/x/pcbnew-copy/demo.kicad_pcb\n")
        self.assertEqual(editor_step.running_editors(lines), [])

    def test_none_running(self):
        self.assertEqual(editor_step.running_editors(""), [])


class GeminiAndCrossProviderStep(unittest.TestCase):
    def setUp(self):
        self.resources = ExitStack()
        self.addCleanup(self.resources.close)
        self.folder = pathlib.Path(self.resources.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.path = self.folder / "motor.kicad_pcb"
        self.path.write_text("(kicad_pcb)", encoding="utf-8")
        self.events = []
        self.images_by_call = []
        self.answer = "NOTHING."

        self.read_paths_by_call = []

        def make_mod(name, binary):
            def invoke(bin_path, prompt, model, effort, repo, env, log, seconds, *, pid=None, images=(), read_paths=()):
                self.events.append((name, bin_path, pid, [p.name for p in images], model, prompt))
                self.images_by_call.append([pathlib.Path(p).resolve() for p in images])
                self.read_paths_by_call.append([pathlib.Path(p).resolve() for p in read_paths])
                log.write_text("fixture", encoding="utf-8")
                return True, (self.answer if pid is None else "done")

            def usage(log, model):
                return {"rounds": [{"input": 10, "output": 2}], "by_model": {model: {}}, "complete": log.is_file()}

            return SimpleNamespace(
                preflight=mock.Mock(return_value=binary),
                run=invoke,
                tokens=usage,
                calls=lambda log, **kw: {**editor_step.calls([]), "route": 1, "route_closed": 1, "look": 1, "looked_after_edit": True},
            )

        self.codex = make_mod("codex", "/opt/codex")
        self.gemini = make_mod("gemini", "/opt/agy")
        self.resources.enter_context(mock.patch.dict(sys.modules, {"routing_codex": self.codex, "routing_gemini": self.gemini}))
        self.resources.enter_context(mock.patch.object(editor_step, "layer_pictures", side_effect=self.capture))
        self.resources.enter_context(mock.patch.object(editor_step, "lock"))
        self.resources.enter_context(mock.patch.object(
            editor_step, "decide", side_effect=lambda *a: ({"passed": True, "why": "editor_work", "before": START,
                                                           "after": check(18, 19, 45)}, [])))
        self.resources.enter_context(mock.patch.object(editor_step, "ROUTING", {"docs": ["truth.md"]}))
        (self.folder / "truth.md").write_text("Review rules.", encoding="utf-8")

    def capture(self, socket, pid, folder, tag, board_name):
        files = [folder / f"{tag}-{layer}.png" for layer in ["top", "bottom"]]
        for file in files:
            file.write_bytes(b"\x89PNG\r\n\x1a\n")
        return files

    def test_cross_provider_codex_operator_with_gemini_reviewer(self):
        args = SimpleNamespace(provider="codex", review_provider="gemini", model=None, review_model=None,
                               effort=None, agent=None, keep_going=False, once=True, minutes=4)
        editor_step.configure_provider(args)
        self.assertEqual((args.model, args.review_provider, args.review_model),
                         (editor_step.CODEX_MODEL, "gemini", editor_step.GEMINI_REVIEW_MODEL))
        self.codex.preflight.assert_called_once()
        self.gemini.preflight.assert_called_once()
        result = editor_step.step(args, "Route A", self.path, SimpleNamespace(pid=975),
                                  "sock", START, [], self.folder, self.folder)[0]
        self.assertTrue(result["passed"])
        self.assertEqual((result["provider"], result["review_provider"], result["review_model"]),
                         ("codex", "gemini", editor_step.GEMINI_REVIEW_MODEL))
        op_ev, rev_ev = self.events
        self.assertEqual((op_ev[0], op_ev[1], op_ev[2]), ("codex", "/opt/codex", 975))
        self.assertEqual((rev_ev[0], rev_ev[1], rev_ev[2], rev_ev[4]), ("gemini", "/opt/agy", None, editor_step.GEMINI_REVIEW_MODEL))
        self.assertIn("read_file", rev_ev[5])
        self.assertEqual(len(self.images_by_call[1]), 4)
        for image_path in self.images_by_call[1]:
            self.assertIn(str(image_path), rev_ev[5])
        self.assertEqual(self.read_paths_by_call[1], [self.path.resolve()])
        self.assertIn(str(self.path.resolve()), rev_ev[5])
        self.assertIn("Do not call read_file on any other path.", rev_ev[5])
        self.assertIn("Do not read AGENTS.md or other repository files", rev_ev[5])
        self.assertIn("run_command", rev_ev[5])
        self.assertIn("disabled in review mode", rev_ev[5])
        self.assertEqual(result["tokens_total"]["input"], 20)

    def test_gemini_operator_with_codex_reviewer_and_custom_model(self):
        args = SimpleNamespace(provider="gemini", review_provider="codex", model=None, review_model=None,
                               effort="high", agent=None, keep_going=False, once=True, minutes=4)
        editor_step.configure_provider(args)
        self.assertEqual((args.model, args.review_provider, args.review_model),
                         (editor_step.GEMINI_MODEL, "codex", editor_step.CODEX_MODEL))
        result = editor_step.step(args, "Route B", self.path, SimpleNamespace(pid=975),
                                  "sock", START, [], self.folder, self.folder)[0]
        self.assertTrue(result["passed"])
        self.assertEqual((result["provider"], result["review_provider"], result["model"], result["review_model"]),
                         ("gemini", "codex", editor_step.GEMINI_MODEL, editor_step.CODEX_MODEL))
        op_ev, rev_ev = self.events
        self.assertEqual((op_ev[0], op_ev[2], op_ev[4]), ("gemini", 975, editor_step.GEMINI_MODEL))
        self.assertIn("read_file", op_ev[5])
        self.assertIn("BypassSandbox: true", op_ev[5])
        self.assertEqual((rev_ev[0], rev_ev[2], rev_ev[4]), ("codex", None, editor_step.CODEX_MODEL))

        opus_args = SimpleNamespace(provider="gemini", review_provider="gemini", model="claude-opus-5-5-high",
                                    review_model=None, effort=None, agent=None, keep_going=False, once=True, minutes=4)
        editor_step.configure_provider(opus_args)
        self.assertEqual(opus_args.model, "claude-opus-5-5-high")

    def test_gemini_default_models_and_explicit_overrides(self):
        cases = [
            (None, None, ("gemini-4-neon-medium", "gemini-4-neon-low")),
            ("gemini-4-neon-high", None, ("gemini-4-neon-high", "gemini-4-neon-low")),
            ("gemini-4-neon-high", "gemini-4-neon-medium", ("gemini-4-neon-high", "gemini-4-neon-medium")),
        ]
        for model, review_model, expected in cases:
            with self.subTest(model=model, review_model=review_model):
                args = SimpleNamespace(provider="gemini", review_provider=None, model=model,
                                       review_model=review_model, effort=None, agent=None,
                                       keep_going=False, once=True, minutes=4)
                editor_step.configure_provider(args)
                self.assertEqual((args.model, args.review_model), expected)

    def test_routing_mode_enforcement_across_ipc_hybrid_and_interactive(self):
        args = SimpleNamespace(provider="codex", review_provider="gemini", model=None, review_model=None,
                               effort=None, agent=None, keep_going=False, once=True, minutes=4)
        editor_step.configure_provider(args)

        # Mode: ipc accepts kicad_lay.py + look, rejects kicad_route.py (mode_violation).
        self.codex.calls = lambda log, **kw: {
            **editor_step.calls([]), "lay": 2, "lay_laid": 1, "ipc_copper_calls": 1, "gui_copper_calls": 0,
            "look": 1, "looked_after_edit": True,
        }
        res_ipc = editor_step.step(args, "Mode: ipc\nRepair: route bus cleanly", self.path, SimpleNamespace(pid=975),
                                   "sock", START, [], self.folder, self.folder)[0]
        self.assertTrue(res_ipc["passed"])
        self.assertEqual(res_ipc["routing_mode"], "ipc")
        self.assertIn("scripts/kicad_lay.py", self.events[0][5])

        self.codex.calls = lambda log, **kw: {
            **editor_step.calls([]), "route": 1, "route_closed": 1, "gui_copper_calls": 1, "ipc_copper_calls": 0,
            "look": 1, "looked_after_edit": True,
        }
        bad_ipc = editor_step.step(args, "Mode: ipc\nRepair: route bus", self.path, SimpleNamespace(pid=975),
                                   "sock", START, [], self.folder, self.folder)[0]
        self.assertFalse(bad_ipc["passed"])
        self.assertEqual((bad_ipc["why"], bad_ipc["routing_mode"]), ("mode_violation", "ipc"))

        # Mode: interactive accepts kicad_route.py, rejects kicad_lay.py.
        self.codex.calls = lambda log, **kw: {
            **editor_step.calls([]), "lay": 1, "lay_laid": 1, "ipc_copper_calls": 1, "gui_copper_calls": 0,
            "look": 1, "looked_after_edit": True,
        }
        bad_inter = editor_step.step(args, "Mode: interactive\nRepair: shove bus", self.path, SimpleNamespace(pid=975),
                                     "sock", START, [], self.folder, self.folder)[0]
        self.assertFalse(bad_inter["passed"])
        self.assertEqual((bad_inter["why"], bad_inter["routing_mode"]), ("mode_violation", "interactive"))

        # Omitted Mode: defaults to hybrid and allows both kicad_lay.py and kicad_route.py, while blocking wrappers.
        self.codex.calls = lambda log, **kw: {
            **editor_step.calls([]), "lay": 1, "lay_laid": 1, "route": 1, "route_closed": 1,
            "ipc_copper_calls": 1, "gui_copper_calls": 1, "look": 1, "looked_after_edit": True,
        }
        res_hybrid = editor_step.step(args, "Repair: mixed bus", self.path, SimpleNamespace(pid=975),
                                      "sock", START, [], self.folder, self.folder)[0]
        self.assertTrue(res_hybrid["passed"])
        self.assertEqual(res_hybrid["routing_mode"], "hybrid")

        self.codex.calls = lambda log, **kw: {
            **editor_step.calls([]), "lay": 1, "lay_laid": 1, "ipc_copper_calls": 1,
            "look": 1, "looked_after_edit": True, "wrapper_calls": 1,
        }
        bad_wrap = editor_step.step(args, "Mode: ipc\nRepair: route bus", self.path, SimpleNamespace(pid=975),
                                    "sock", START, [], self.folder, self.folder)[0]
        self.assertFalse(bad_wrap["passed"])
        self.assertEqual(bad_wrap["why"], "unauthorized_wrapper")

    def test_mode_guidance_ipc_hybrid_and_board_context_with_plan(self):
        for mode in ("ipc", "hybrid"):
            g = editor_step.mode_guidance(mode, "python", "scripts", 975)
            self.assertIn("--json", g)
            self.assertIn("--stdin", g)
            self.assertIn("write_file", g)
            self.assertNotIn("batch --file", g)
            self.assertIn("0.15 mm", g)
            self.assertIn("0.02 mm", g)
            self.assertIn('"steps"', g)

        # Prompt tells Codex & Gemini not to probe kicad_ipc_status.py at startup, writes board-context.txt,
        # inlines routing_agent.plan for operator & reviewer, and includes plan in Gemini review read_paths.
        pcb_with_pads = """(kicad_pcb
  (version 20260206)
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal))
  (footprint "t:J1" (layer "F.Cu") (at 10 10)
    (property "Reference" "J1" (at 0 0 0) (layer "F.SilkS"))
    (fp_line (start -1 -1) (end 1 1) (layer "F.CrtYd"))
    (pad "1" smd rect (at 0 0) (size 0.6 1.0) (layers "F.Cu") (net "/SIG"))
  )
  (footprint "t:J2" (layer "F.Cu") (at 10 20)
    (property "Reference" "J2" (at 0 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 0.6 1.0) (layers "F.Cu") (net "/SIG"))
  )
)"""
        self.path.write_text(pcb_with_pads, encoding="utf-8")
        (self.folder / "plan.md").write_text("Route /SIG on F.Cu straight down.", encoding="utf-8")
        self.resources.enter_context(mock.patch.object(editor_step, "ROUTING", {"docs": ["truth.md"], "plan": "plan.md"}))

        args = SimpleNamespace(provider="gemini", review_provider="gemini", model=None, review_model=None,
                               effort=None, agent=None, keep_going=False, once=True, minutes=4)
        editor_step.configure_provider(args)
        ipc_calls = {**editor_step.calls([]), "lay": 1, "lay_laid": 1, "ipc_copper_calls": 1, "look": 1, "looked_after_edit": True}
        self.gemini.calls = lambda log, **kw: dict(ipc_calls)
        self.codex.calls = lambda log, **kw: dict(ipc_calls)
        res = editor_step.step(args, "Mode: ipc\nRepair: route /SIG", self.path, SimpleNamespace(pid=975),
                               "sock", START, [], self.folder, self.folder)[0]
        self.assertTrue(res["passed"])
        op_prompt = self.events[0][5]
        rev_prompt = self.events[1][5]
        self.assertIn("do not call kicad_ipc_status.py at startup", op_prompt)
        self.assertIn("Auto-generated board geometry brief", op_prompt)
        self.assertIn("Board routing plan (plan.md)", op_prompt)
        self.assertIn("Route /SIG on F.Cu straight down.", op_prompt)
        self.assertIn("Board reference docs (read only if needed): truth.md.", op_prompt)
        self.assertTrue((self.folder / "board-context.txt").is_file())
        self.assertIn("plan.md", rev_prompt)
        self.assertIn((self.folder / "plan.md").resolve(), self.read_paths_by_call[1])

        self.events.clear()
        codex_args = SimpleNamespace(provider="codex", review_provider="gemini", model=None, review_model=None,
                                     effort=None, agent=None, keep_going=False, once=True, minutes=4)
        editor_step.configure_provider(codex_args)
        res_codex = editor_step.step(codex_args, "Mode: ipc\nRepair: route /SIG", self.path, SimpleNamespace(pid=975),
                                     "sock", START, [], self.folder, self.folder)[0]
        self.assertTrue(res_codex["passed"])
        codex_prompt = self.events[0][5]
        self.assertIn("do not call kicad_ipc_status.py at startup", codex_prompt)
        self.assertIn("Auto-generated board geometry brief", codex_prompt)
        self.assertIn("Board routing plan (plan.md)", codex_prompt)


class StepExtensionAndCandidateTests(unittest.TestCase):
    def _args(self, *, minutes=10):
        return SimpleNamespace(
            provider="gemini",
            review_provider="gemini",
            model="gemini-4-neon",
            review_model="gemini-4-neon",
            effort="medium",
            minutes=minutes,
            agent="",
            once=False,
            gemini_binary="gemini",
            codex_binary="codex",
        )

    def test_clean_partial_progress_grants_extension_and_archives_unfinished_candidate(self):
        import threading
        import routing_gemini

        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td).resolve()
            pcb = root / "b.kicad_pcb"
            pcb.write_text("(kicad_pcb (version 20241229))\n", encoding="utf-8")
            folder = root / "step"
            folder.mkdir()
            png = folder / "round2-candidate-top.png"
            png.write_bytes(b"\x89PNG\r\n\x1a\n")
            (folder / "drc-end.json").write_text("{}", encoding="utf-8")
            (folder / "quality-round1.json").write_text("{}", encoding="utf-8")
            (folder / "quality-round2.json").write_text("{}", encoding="utf-8")

            s1 = '(kicad_pcb (version 20241229)\n\t(segment (start 0 0) (end 5 0) (width 0.25) (layer "F.Cu") (net "/SIG1") (uuid "s1"))\n)\n'
            s2 = '(kicad_pcb (version 20241229)\n\t(segment (start 0 0) (end 5 0) (width 0.25) (layer "F.Cu") (net "/SIG1") (uuid "s1"))\n\t(segment (start 0 2) (end 5 2) (width 0.25) (layer "F.Cu") (net "/SIG2") (uuid "s2"))\n)\n'
            clock_now = [1800000000.0]

            def fake_gemini_run(_bin, _prompt, *_args, **_kw):
                if clock_now[0] < 1800000500.0:
                    pcb.write_text(s1, encoding="utf-8")
                    clock_now[0] = 1800000590.0  # within < 30s of deadline (started=1800000000 + 600s) -> triggers 1 extension
                else:
                    pcb.write_text(s2, encoding="utf-8")
                    clock_now[0] += 400.0  # past extended deadline -> stops after extension
                return True, ""

            fake_calls = {
                "route": 0, "route_closed": 0, "route_refused": 0, "route_by_status": {},
                "edit": 0, "edit_by_verb": {}, "lay": 1, "lay_laid": 1, "lay_refused": 0, "lay_by_status": {"laid": 1},
                "look": 1, "looked_after_edit": True, "gui_copper_calls": 0, "ipc_copper_calls": 1,
                "wrapper_calls": 0, "other": 0, "repeats": {}, "first_editor_failure": None,
            }
            done = threading.Event()
            done.set()
            with (
                mock.patch.object(editor_step, "ROUTING", {"tasks": True}),
                mock.patch.object(editor_step, "saved_connection", side_effect=[False, False, True, False, True, False]),
                mock.patch.object(editor_step, "layer_pictures", return_value=[png]),
                mock.patch.object(editor_step, "lock"),
                mock.patch.object(editor_step.board_context, "render_board_context", return_value=("", "")),
                mock.patch.object(editor_step, "operator_docs_text", return_value=""),
                mock.patch.object(routing_gemini, "run", side_effect=fake_gemini_run) as gemini_run,
                mock.patch.object(routing_gemini, "calls", return_value=fake_calls),
                mock.patch.object(routing_gemini, "tokens", return_value={"rounds": [], "by_model": {}, "complete": True}),
                mock.patch.object(editor_step.time, "time", side_effect=lambda: clock_now[0]),
                mock.patch.object(editor_step, "decide", return_value=({"passed": True, "why": "editor_work", "after": {"open": 1, "errors": 0, "findings": 0}, "new_findings": []}, {})),
            ):
                task = "Mode: ipc\nConnection: J1.1 J2.1 /SIG1\nConnection: J1.2 J2.2 /SIG2\n"
                res, _ = editor_step.step_rounds(
                    self._args(minutes=10), task, pcb, SimpleNamespace(pid=123), "ipc://sock",
                    {"open": 2, "errors": 0, "findings": 0}, {}, folder, root, done,
                )

            self.assertEqual(gemini_run.call_count, 2)
            self.assertEqual(res["rounds"], 2)
            self.assertFalse(res["passed"])
            self.assertEqual(res["why"], "connection_open")
            self.assertEqual(res["candidate"]["archive"], "candidate-round2.zip")
            self.assertTrue((folder / "candidate-round2.zip").is_file())


class PourEditorStepTests(unittest.TestCase):
    def _args(self):
        return SimpleNamespace(
            provider="gemini",
            review_provider="gemini",
            model="gemini-4-neon-medium",
            review_model="gemini-4-neon-low",
            effort="medium",
            minutes=10,
            agent="",
            once=False,
            gemini_binary="gemini",
            codex_binary="codex",
        )

    def test_copper_and_moved_table_track_zone_changes(self):
        before = '(kicad_pcb\n  (net 1 "GND")\n)\n'
        after = (
            '(kicad_pcb\n  (net 1 "GND")\n'
            '  (zone (net 1) (net_name "GND") (layers "In1.Cu") (uuid "z1")\n'
            '    (polygon (pts (xy 0 0) (xy 10 0) (xy 10 10) (xy 0 10)))\n'
            '    (filled_polygon (layer "In1.Cu") (pts (xy 0 0) (xy 10 0) (xy 10 10)))\n'
            '  )\n)\n'
        )
        c_after = editor_step.copper(after)
        self.assertEqual(c_after["GND"]["zones"], 1)
        table = editor_step.moved_table(before, after)
        self.assertIn("GND: length 0.0 to 0.0 mm", table)
        self.assertIn("zones 0 to 1 (In1.Cu)", table)

    def test_layer_pictures_with_extra_layers_and_review_six_pictures(self):
        import types

        layer_changes = []
        fake_board = SimpleNamespace(
            get_active_layer=lambda: "BL_F_Cu",
            set_active_layer=lambda lid: layer_changes.append(lid),
        )
        fake_kipy = types.ModuleType("kipy")
        fake_kipy.KiCad = lambda socket_path, timeout_ms=60000: SimpleNamespace(get_board=lambda: fake_board)
        fake_pb = types.ModuleType("kipy.proto.board.board_types_pb2")
        fake_pb.BoardLayer = SimpleNamespace(BL_F_Cu="BL_F_Cu", BL_B_Cu="BL_B_Cu")
        fake_bl = types.ModuleType("kipy.util.board_layer")
        fake_bl.layer_from_canonical_name = lambda name: f"BL_{name.replace('.', '_')}"
        fake_proto = types.ModuleType("kipy.proto")
        fake_proto_b = types.ModuleType("kipy.proto.board")
        fake_util = types.ModuleType("kipy.util")

        png = b"\x89PNG\r\n\x1a\n" + b"0" * 16
        with tempfile.TemporaryDirectory() as td:
            folder = pathlib.Path(td)
            board_p = folder / "b.kicad_pcb"
            board_p.write_text("(kicad_pcb)\n", encoding="utf-8")

            def fake_pic(_pid, out, _stem):
                out.write_bytes(png)

            with (
                mock.patch.dict(sys.modules, {
                    "kipy": fake_kipy,
                    "kipy.proto": fake_proto,
                    "kipy.proto.board": fake_proto_b,
                    "kipy.proto.board.board_types_pb2": fake_pb,
                    "kipy.util": fake_util,
                    "kipy.util.board_layer": fake_bl,
                }),
                mock.patch.object(editor_step, "picture_of_editor", side_effect=fake_pic),
                mock.patch.object(editor_step.time, "sleep"),
            ):
                pics = editor_step.layer_pictures("ipc://s", 9, folder, "round1", "b", extra_layers=("In1.Cu", "F.Cu"))
            self.assertEqual([p.name for p in pics], ["round1-top.png", "round1-bottom.png", "round1-In1_Cu.png"])
            self.assertEqual(layer_changes, ["BL_F_Cu", "BL_B_Cu", "BL_In1_Cu", "BL_F_Cu"])

            # 6 pictures (start top/bot/In1 + after top/bot/In1) pass review(); 5 pictures fail
            start_pics = []
            for name in ("start-top.png", "start-bottom.png", "start-In1_Cu.png"):
                p = folder / name
                p.write_bytes(png)
                start_pics.append(p)
            import routing_gemini
            with (
                mock.patch.object(editor_step, "ROUTING", {"docs": []}),
                mock.patch.object(routing_gemini, "run", return_value=(True, "NOTHING.")),
            ):
                open_, ans = editor_step.review(
                    board_p, "Mode: ipc\nPour: In1.Cu GND\n", start_pics + pics, "table", [],
                    "gemini-4-neon-low", "low", folder, {}, folder / "rev.jsonl", 60,
                    review_provider="gemini", gemini_binary="gemini",
                )
            self.assertEqual((open_, ans), ([], "NOTHING."))
            with self.assertRaisesRegex(ValueError, "matching front, back"):
                editor_step.review(
                    board_p, "task", start_pics + pics[:-1], "table", [],
                    "gemini-4-neon-low", "low", folder, {}, folder / "rev2.jsonl", 60,
                    review_provider="gemini", gemini_binary="gemini",
                )

    def test_step_rounds_pour_only_task_retry_and_adoption(self):
        import threading
        import routing_evidence
        import routing_gemini

        base_pcb = (
            '(kicad_pcb (version 20260206)\n'
            '  (layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (31 "B.Cu" signal))\n'
            '  (net 0 "") (net 1 "GND")\n)\n'
        )
        unfilled_pcb = (
            '(kicad_pcb (version 20260206)\n'
            '  (layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (31 "B.Cu" signal))\n'
            '  (net 0 "") (net 1 "GND")\n'
            '  (zone (net 1) (net_name "GND") (layers "In1.Cu") (uuid "z1")\n'
            '    (polygon (pts (xy 0 0) (xy 20 0) (xy 20 20) (xy 0 20)))\n'
            '  )\n)\n'
        )
        poured_pcb = (
            '(kicad_pcb (version 20260206)\n'
            '  (layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (31 "B.Cu" signal))\n'
            '  (net 0 "") (net 1 "GND")\n'
            '  (zone (net 1) (net_name "GND") (layers "In1.Cu") (uuid "z1")\n'
            '    (polygon (pts (xy 0 0) (xy 20 0) (xy 20 20) (xy 0 20)))\n'
            '    (filled_polygon (layer "In1.Cu") (pts (xy 0 0) (xy 20 0) (xy 20 20)))\n'
            '  )\n)\n'
        )
        task = "Mode: ipc\nPour: In1.Cu GND\n\nPour the solid GND reference plane on In1.Cu.\n"
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td).resolve()
            pcb = root / "b.kicad_pcb"
            pcb.write_bytes(base_pcb.encode("utf-8"))
            folder = root / "step"
            folder.mkdir()
            png = b"\x89PNG\r\n\x1a\n" + b"0" * 16
            round_num = [0]

            def fake_gemini(bin_, prompt, model, effort, repo, env, log, seconds, pid, **_kw):
                round_num[0] += 1
                return True, "DONE"

            def fake_editor_board(_sock, save=False, refill=False):
                if save:
                    if round_num[0] == 1:
                        pcb.write_bytes(unfilled_pcb.encode("utf-8"))
                    else:
                        pcb.write_bytes(poured_pcb.encode("utf-8"))
                return pcb

            def fake_pics(_sock, _pid, fld, tag, _stem, **kwargs):
                self.assertEqual(kwargs.get("extra_layers"), ("In1.Cu",))
                out = []
                for name in (f"{tag}-top.png", f"{tag}-bottom.png", f"{tag}-In1_Cu.png"):
                    p = fld / name
                    p.write_bytes(png)
                    out.append(p)
                return out

            def fake_review(p, *_args, **_kw):
                (folder / "review1.jsonl").write_text("{}", encoding="utf-8")
                return [], "NOTHING."

            done = threading.Event()
            done.set()
            # Exercise real decide() using mocked design_check / audits / editor_board
            with (
                mock.patch.object(editor_step, "ROUTING", {"tasks": True}),
                mock.patch.object(editor_step, "layer_pictures", side_effect=fake_pics),
                mock.patch.object(editor_step, "lock"),
                mock.patch.object(editor_step, "editor_board", side_effect=fake_editor_board),
                mock.patch.object(editor_step, "same_board", return_value=True),
                mock.patch.object(editor_step, "design_check", return_value={"open": 5, "errors": 0, "findings": 0}),
                mock.patch.object(editor_step, "audits", return_value={}),
                mock.patch.object(editor_step.board_context, "render_board_context", return_value=("ctx", "ctx")),
                mock.patch.object(editor_step, "operator_docs_text", return_value=""),
                mock.patch.object(routing_gemini, "run", side_effect=fake_gemini),
                mock.patch.object(routing_gemini, "calls", return_value={"route": 0, "route_closed": 0, "route_refused": 0, "route_by_status": {}, "lay": 1, "lay_laid": 1, "lay_refused": 0, "lay_by_status": {"laid": 1}, "ipc_copper_calls": 1, "gui_copper_calls": 0, "edit": 0, "edit_by_verb": {}, "look": 1, "other": 0, "repeats": {}, "looked_after_edit": True, "editor_mismatches": 0, "wrapper_calls": 0}),
                mock.patch.object(routing_gemini, "tokens", return_value={"rounds": [{"input": 50, "output": 10, "thinking": 0, "cache_read": 0, "cache_write": 0}], "by_model": {}, "complete": True}),
                mock.patch.object(editor_step, "review", side_effect=fake_review),
            ):
                res, _aud = editor_step.step_rounds(
                    self._args(), task, pcb, SimpleNamespace(pid=123, poll=lambda: None), "ipc://sock",
                    {"open": 5, "errors": 0, "findings": 0}, {}, folder, root, done,
                )

            self.assertTrue(res["passed"])
            self.assertEqual(res["rounds"], 2)
            self.assertEqual(res["first_check_failure"]["why"], "pour_missing")
            self.assertEqual(res["pours"][0]["filled"], True)
            sha_after = res["reviewed_sha256"]
            step_rec = dict(res, task="01-pour.txt", kept=True, before={"open": 5, "errors": 0, "findings": 0})
            summary = {"tasks": 1, "kept": 1, "stopped": "", "provider": "gemini", "review_provider": "gemini"}
            self.assertEqual(
                routing_evidence.adoption_problem([step_rec], summary, sha_after, "gemini", expected_tasks={"01-pour.txt": task}),
                "",
            )

    def test_final_pours_verifies_preservation(self):
        pcb_ok = (
            '(kicad_pcb (version 20260206)\n'
            '  (layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (31 "B.Cu" signal))\n'
            '  (net 1 "GND")\n'
            '  (zone (net 1) (net_name "GND") (layers "In1.Cu")\n'
            '    (polygon (pts (xy 0 0) (xy 10 0) (xy 10 10) (xy 0 10)))\n'
            '    (filled_polygon (layer "In1.Cu") (pts (xy 0 0) (xy 10 0) (xy 10 10)))\n'
            '  )\n)\n'
        )
        with tempfile.TemporaryDirectory() as td:
            pcb = pathlib.Path(td) / "b.kicad_pcb"
            pcb.write_bytes(pcb_ok.encode("utf-8"))
            results = [
                {"task": "01-pour.txt", "kept": True, "pours": [{"layers": ["In1.Cu"], "net": "GND"}]},
                {"task": "02-route.txt", "kept": True},
            ]
            fp = editor_step.final_pours(pcb, results)
            self.assertEqual(fp["pours"], {"01-pour.txt": [{"layers": ["In1.Cu"], "net": "GND"}]})
            pcb.write_text('(kicad_pcb (version 20260206) (net 1 "GND"))\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Previously kept pour disappeared"):
                editor_step.final_pours(pcb, results)

    def test_changed_copper_details_formats_and_truncates_without_fill_bulk(self):
        before = (
            '(kicad_pcb (version 20260206)\n'
            '  (net 0 "") (net 1 "+5V") (net 2 "GND")\n'
            '  (segment (start 10 20) (end 15 20) (width 0.5) (layer "F.Cu") (net 1))\n'
            '  (segment (start 50 50) (end 55 50) (width 0.25) (layer "B.Cu") (net 2))\n'
            '  (zone (net 2) (net_name "GND") (layers "In1.Cu")\n'
            '    (polygon (pts (xy 0 0) (xy 100 0) (xy 100 100) (xy 0 100)))\n'
            '    (filled_polygon (layer "In1.Cu") (pts (xy 0.1 0.1) (xy 99.9 0.1) (xy 99.9 99.9) (xy 0.1 99.9)))\n'
            '  )\n)\n'
        )
        # Reversed identical segment on GND + different fill polygon points should NOT count as changed;
        # new +5V segments and via MUST appear without dumping filled_polygon coordinates.
        after = (
            '(kicad_pcb (version 20260206)\n'
            '  (net 0 "") (net 1 "+5V") (net 2 "GND")\n'
            '  (segment (start 10 20) (end 15 20) (width 0.5) (layer "F.Cu") (net 1))\n'
            '  (segment (start 15 20) (end 18 23) (width 0.5) (layer "F.Cu") (net 1))\n'
            '  (segment (start 18 23) (end 24 23) (width 0.35) (layer "B.Cu") (net 1))\n'
            '  (via (at 18 23) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))\n'
            '  (segment (start 55 50) (end 50 50) (width 0.25) (layer "B.Cu") (net 2))\n'
            '  (zone (net 2) (net_name "GND") (layers "In1.Cu")\n'
            '    (polygon (pts (xy 0 0) (xy 100 0) (xy 100 100) (xy 0 100)))\n'
            '    (filled_polygon (layer "In1.Cu") (pts (xy 0.2 0.2) (xy 99.8 0.2) (xy 99.8 99.8) (xy 0.2 99.8)))\n'
            '  )\n)\n'
        )
        details = editor_step.changed_copper_details(before, after)
        self.assertIn("Net +5V: +2/-0 segments, +1/-0 vias", details)
        self.assertIn("+ seg F.Cu w=0.5 (15,20)->(18,23)", details)
        self.assertIn("+ seg B.Cu w=0.35 (18,23)->(24,23)", details)
        self.assertIn("+ via (18,23) d=0.6/0.3 (F.Cu,B.Cu)", details)
        self.assertNotIn("Net GND", details)
        self.assertNotIn("99.8", details)

        capped = editor_step.changed_copper_details(before, after, max_items=2)
        self.assertIn("... (+1 more copper items in saved PCB)", capped)

    def test_ipc_mode_prompt_omits_gui_edit_dialogs_and_guides_view_inspection(self):
        import threading
        import routing_gemini

        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td).resolve()
            pcb = root / "b.kicad_pcb"
            pcb.write_text('(kicad_pcb (version 20260206))\n', encoding="utf-8")
            folder = root / "step"
            folder.mkdir()
            seen_prompts = []

            def fake_gemini(bin_, prompt, *_a, **_kw):
                seen_prompts.append(prompt)
                return True, "DONE"

            done = threading.Event()
            done.set()
            png = b"\x89PNG\r\n\x1a\n" + b"0" * 16

            def fake_pics(_sock, _pid, fld, tag, _stem):
                out = []
                for name in (f"{tag}-top.png", f"{tag}-bottom.png"):
                    p = fld / name
                    p.write_bytes(png)
                    out.append(p)
                return out

            with (
                mock.patch.object(editor_step, "ROUTING", {"tasks": True}),
                mock.patch.object(editor_step, "layer_pictures", side_effect=fake_pics),
                mock.patch.object(editor_step, "lock"),
                mock.patch.object(editor_step.board_context, "render_board_context", return_value=("", "")),
                mock.patch.object(editor_step, "operator_docs_text", return_value=""),
                mock.patch.object(routing_gemini, "run", side_effect=fake_gemini),
                mock.patch.object(routing_gemini, "calls", return_value=editor_step.calls([])),
                mock.patch.object(routing_gemini, "tokens", return_value={"rounds": [], "by_model": {}, "complete": True}),
                mock.patch.object(editor_step, "decide", return_value=({"passed": False, "why": "check_failed", "before": START, "after": START}, None)),
            ):
                editor_step.step_rounds(
                    self._args(), "Mode: ipc\nPour: F.Cu GND\n", pcb,
                    SimpleNamespace(pid=123, poll=lambda: None), "ipc://sock",
                    START, {}, folder, root, done,
                )

            ipc_prompt = seen_prompts[0]
            self.assertNotIn("GUI dialogs when needed", ipc_prompt)
            self.assertIn('mac.py menu "PCB Editor" "View" "Zoom to Fit"', ipc_prompt)
            self.assertIn("NEVER use `kicad_hover_key.py`, `type`, `mouse`", ipc_prompt)
            self.assertIn('"to_via"', ipc_prompt)

    def test_step_timing_and_run_totals_aggregate_timing_and_repeated_refusals(self):
        step_one = {
            "tokens_total": dict.fromkeys(editor_step.TOKEN_KINDS, 1),
            "timing": {"total_s": 40.0, "llm_s": 30.0, "tool_s": 8.0, "review_s": 1.5,
                       "harness_s": 0.5, "refusal_s": 2.0, "thinking_tokens": 1200},
            "calls": {
                **editor_step.calls([]),
                "lay": 2, "lay_laid": 1, "lay_refused": 1,
                "help_calls": 1, "refusal_seconds": 2.0,
                "refusal_reasons": {"kicad_lay: bad_request: missing to_via": {"count": 1, "seconds": 2.0}},
            },
        }
        step_two = {
            "tokens_total": dict.fromkeys(editor_step.TOKEN_KINDS, 2),
            "timing": {"total_s": 25.0, "llm_s": 15.0, "tool_s": 7.0, "review_s": 2.5,
                       "harness_s": 0.5, "refusal_s": 3.0, "thinking_tokens": 800},
            "calls": {
                **editor_step.calls([]),
                "lay": 3, "lay_laid": 1, "lay_refused": 2,
                "help_calls": 0, "refusal_seconds": 3.0,
                "refusal_reasons": {
                    "kicad_lay: bad_request: missing to_via": {"count": 1, "seconds": 1.5},
                    "kicad_lay: not_in_pour: outside pour": {"count": 1, "seconds": 1.5},
                },
            },
        }
        totals = editor_step.run_totals([step_one, step_two])
        self.assertEqual(totals["timing"], {
            "total_s": 65.0, "llm_s": 45.0, "tool_s": 15.0, "review_s": 4.0,
            "harness_s": 1.0, "refusal_s": 5.0, "thinking_tokens": 2000,
        })
        self.assertEqual(totals["refusals"]["help_calls"], 1)
        self.assertEqual(totals["refusals"]["refusal_seconds"], 5.0)
        self.assertEqual(totals["refusals"]["repeated_across_steps"], [
            {"reason": "kicad_lay: bad_request: missing to_via", "steps": 2, "count": 2, "seconds": 3.5},
        ])


class V27EditorStepTests(unittest.TestCase):
    def test_mode_guidance_ipc_centered_flag_and_to_point_example(self) -> None:
        default_text = editor_step.mode_guidance("ipc", "python3", "scripts", 123, centered=False)
        self.assertNotIn('"centered_exits":true', default_text)
        self.assertIn('"to_point"', default_text)
        self.assertIn("Only set `\"centered_exits\": true` when `Pad exits: centered` is required", default_text)

        centered_text = editor_step.mode_guidance("ipc", "python3", "scripts", 123, centered=True)
        self.assertIn('"centered_exits":true', centered_text)
        self.assertIn('"to_point"', centered_text)

    def test_run_totals_groups_normalized_repeats_and_keeps_raw_by_reason(self) -> None:
        r1 = "kicad_lay: bad_request: U2.6: neither short edge faces (143.137, 101.125)"
        r2 = "kicad_lay: bad_request: U2.5: neither short edge faces (143.137, 100.475)"
        results = [
            {
                "passed": True,
                "tokens_total": None,
                "calls": {
                    **editor_step.calls([]),
                    "help_calls": 0,
                    "refusal_seconds": 8.0,
                    "refusal_reasons": {r1: {"count": 2, "seconds": 8.0}},
                },
            },
            {
                "passed": True,
                "tokens_total": None,
                "calls": {
                    **editor_step.calls([]),
                    "help_calls": 1,
                    "refusal_seconds": 4.0,
                    "refusal_reasons": {r2: {"count": 1, "seconds": 4.0}},
                },
            },
        ]
        totals = editor_step.run_totals(results)
        self.assertIn(r1, totals["refusals"]["by_reason"])
        self.assertIn(r2, totals["refusals"]["by_reason"])
        self.assertEqual(
            totals["refusals"]["repeated_across_steps"],
            [{
                "reason": "kicad_lay: bad_request: <pad>: neither short edge faces (<x,y>)",
                "steps": 2,
                "count": 3,
                "seconds": 12.0,
            }],
        )

    def test_design_check_and_check_passes_for_all_smd_isolated_copper(self) -> None:
        import json
        import tempfile

        def run_drc_payload(payload):
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = pathlib.Path(tmp)
                out_path = tmp_path / "drc.json"
                board_path = tmp_path / "b.kicad_pcb"

                def fake_run(_cmd, **_kw):
                    out_path.write_text(json.dumps(payload), encoding="utf-8")
                    return SimpleNamespace(returncode=0)

                with mock.patch.object(editor_step.subprocess, "run", side_effect=fake_run):
                    return editor_step.design_check(board_path, out_path)

        base = run_drc_payload({
            "unconnected_items": [{"a": 1}, {"a": 2}],
            "violations": [],
        })
        self.assertEqual(base, {"open": 2, "errors": 0, "findings": 0})
        self.assertNotIn("isolated_copper", base)

        with_iso = run_drc_payload({
            "unconnected_items": [{"a": 1}, {"a": 2}],
            "violations": [
                {"severity": "warning", "type": "isolated_copper"},
                {"severity": "warning", "type": "isolated_copper"},
            ],
        })
        self.assertEqual(with_iso, {"open": 2, "errors": 0, "findings": 2, "isolated_copper": 2})

        self.assertTrue(editor_step.check_passes(base, with_iso, pours_changed=True))
        self.assertFalse(editor_step.check_passes(base, with_iso, pours_changed=False))

        with_other_warn = {"open": 2, "errors": 0, "findings": 3, "isolated_copper": 2}
        self.assertFalse(editor_step.check_passes(base, with_other_warn, pours_changed=True))

        with_error = {"open": 2, "errors": 1, "findings": 3, "isolated_copper": 2}
        self.assertFalse(editor_step.check_passes(base, with_error, pours_changed=True))


class V28ExtraToolsTests(unittest.TestCase):
    def test_calls_tracks_other_by_cmd_and_run_totals_emits_extra_tools(self) -> None:
        import json

        events = [
            json.dumps({
                "message": {
                    "content": [
                        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "python3 scripts/kicad_edit.py list --vias"}},
                        {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "python3 -c 'print(1)'"}},
                        {"type": "tool_use", "id": "t3", "name": "Bash", "input": {"command": "grep -n GND board-context.txt"}},
                    ]
                },
            }),
            json.dumps({"message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "[]"}]}}),
            json.dumps({"message": {"content": [{"type": "tool_result", "tool_use_id": "t2", "content": "1"}]}}),
            json.dumps({"message": {"content": [{"type": "tool_result", "tool_use_id": "t3", "content": "ok"}]}}),
        ]
        c = editor_step.calls(events)
        self.assertEqual(c["edit_by_verb"], {"list": 1})
        self.assertEqual(c["other"], 2)
        self.assertEqual(c["other_by_cmd"], {"grep": 1, "python3": 1})

        totals_with = editor_step.run_totals([
            {"tokens_total": None, "calls": {**editor_step.calls([]), "edit_by_verb": {"list": 3, "delete": 1}, "other_by_cmd": {"python3": 1}}},
            {"tokens_total": None, "calls": {**editor_step.calls([]), "edit_by_verb": {"list": 2}, "other_by_cmd": {"grep": 1}}},
        ])
        self.assertEqual(totals_with["extra_tools"], {"edit_by_verb": {"delete": 1, "list": 5}, "other_by_cmd": {"grep": 1, "python3": 1}})

        totals_empty = editor_step.run_totals([{"tokens_total": None, "calls": editor_step.calls([])}])
        self.assertNotIn("extra_tools", totals_empty)


class V281FixesTests(unittest.TestCase):
    def test_audits_preserves_coordinates_so_moved_warning_hits_finding_details_and_evidence(self) -> None:
        import json
        import tempfile
        import routing_evidence

        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td).resolve()
            pcb = root / "b.kicad_pcb"
            pcb.write_text("(kicad_pcb (version 20241229))\n", encoding="utf-8")
            folder = root / "step"
            folder.mkdir()

            old_findings = [
                {"severity": "warning", "rule": "micro_segment", "net": "/AWAKE", "layer": "F.Cu",
                 "point_mm": [10.0, 20.0], "message": "0.1000 mm"},
                {"severity": "warning", "rule": "micro_segment", "net": "/AWAKE", "layer": "F.Cu",
                 "point_mm": [30.0, 40.0], "message": "0.1100 mm"},
                {"severity": "warning", "rule": "micro_segment", "net": "/AWAKE", "layer": "B.Cu",
                 "point_mm": [50.0, 60.0], "message": "0.1200 mm"},
            ]
            moved_item = {
                "severity": "warning", "rule": "micro_segment", "net": "/AWAKE", "layer": "B.Cu",
                "point_mm": [55.0, 62.0], "message": "0.1250 mm",
            }
            new_findings_list = old_findings[:2] + [moved_item]

            def fake_offline(_script, *_args, **_kw):
                out_path = _args[_args.index("--output") + 1]
                payload = old_findings if out_path.name == "quality-start.json" else new_findings_list
                out_path.write_text(json.dumps({"findings": payload}), encoding="utf-8")

            with (
                mock.patch.object(editor_step.validate_board, "ELECTRICAL", None),
                mock.patch.object(editor_step.validate_board, "kicad_offline", side_effect=fake_offline),
            ):
                start_a = editor_step.audits(pcb, folder, "start")
                end_a = editor_step.audits(pcb, folder, "end")

            self.assertEqual(len(start_a), 3)
            self.assertEqual(len(end_a), 3)
            added = editor_step.new_findings(start_a, end_a)
            expected_detail = routing_evidence.route_quality_warning_detail(moved_item)
            self.assertEqual(added, [expected_detail])
            details = editor_step.finding_details(folder, "end", added)
            self.assertEqual(details, [expected_detail])

            # Legacy short-label matching still works alongside new exact-coordinate matching in both editor_step and routing_evidence.
            self.assertTrue(any(d == added[0] or d.startswith(added[0] + ",") for d in details))
            legacy_added = ["route quality: warning micro_segment on /AWAKE"]
            self.assertEqual(editor_step.finding_details(folder, "end", legacy_added), [routing_evidence.route_quality_warning_detail(old_findings[0])])
            board_sha = "a" * 64
            base_step = {
                "task": "01-a", "kept": True, "why": "warning_accepted", "reviews": 1, "review_open": [],
                "provider": "gemini", "model": "gemini-4-neon-medium",
                "review_provider": "gemini", "review_model": "gemini-4-neon-low",
                "matching_geometry": [expected_detail], "reviewed_sha256": board_sha, "pad_exits": [],
                "review_receipt": {"schema": "board-graph/routing-review/v1", "board_sha256": board_sha,
                                   "reviewer": "gemini-4-neon-low", "answer": "FINE AS IS: Warning 1: short stub cleanly shielded.",
                                   "earlier": [], "warnings": [expected_detail]},
            }
            summary = {"kept": 1, "tasks": 1}
            self.assertEqual(routing_evidence.adoption_problem([{**base_step, "new_findings": [expected_detail]}], summary, board_sha, "gemini"), "")
            self.assertEqual(routing_evidence.adoption_problem([{**base_step, "new_findings": [expected_detail.split(",", 1)[0]]}], summary, board_sha, "gemini"), "")

    def test_check_passes_and_decide_accept_zero_open_audit_reduction_and_reject_no_reduction(self) -> None:
        import json
        import tempfile

        start = {"open": 0, "errors": 0, "findings": 0}
        same_drc = {"open": 0, "errors": 0, "findings": 0}
        self.assertFalse(editor_step.check_passes(start, same_drc))
        self.assertTrue(editor_step.check_passes(start, same_drc, audits_reduced=True))
        self.assertFalse(editor_step.check_passes(start, {"open": 0, "errors": 1, "findings": 1}, audits_reduced=True))

        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td).resolve()
            pcb = root / "b.kicad_pcb"
            pcb.write_text("(kicad_pcb (version 20241229))\n", encoding="utf-8")
            folder = root / "step"
            folder.mkdir()

            start_audits = [
                "route quality: warning micro_segment on /A, layer F.Cu at [1.0, 2.0]: a",
                "route quality: warning micro_segment on /B, layer F.Cu at [3.0, 4.0]: b",
            ]
            end_count = [1]

            def fake_audits(_path, _folder, _tag):
                return start_audits[: end_count[0]]

            with (
                mock.patch.object(editor_step, "lock"),
                mock.patch.object(editor_step, "editor_board", return_value=str(pcb)),
                mock.patch.object(editor_step, "same_board", return_value=True),
                mock.patch.object(editor_step, "design_check", return_value=same_drc),
                mock.patch.object(editor_step, "audits", side_effect=fake_audits) as audits_mock,
            ):
                proc = SimpleNamespace(pid=1, poll=lambda: None)
                res_ok, out_audits = editor_step.decide(
                    pcb, start, start_audits, proc, "sock", [], True, folder, "round1",
                )
                self.assertTrue(res_ok["passed"])
                self.assertEqual(res_ok["why"], "editor_work")
                self.assertEqual(len(out_audits), 1)
                self.assertEqual(audits_mock.call_count, 1)

                end_count[0] = 2
                res_fail, out_none = editor_step.decide(
                    pcb, start, start_audits, proc, "sock", [], True, folder, "round2",
                )
                self.assertFalse(res_fail["passed"])
                self.assertEqual(res_fail["why"], "check_failed")
                self.assertIsNone(out_none)

    def test_missing_visual_look_grants_grace_extension_and_uses_compact_retry_prompt(self) -> None:
        import json
        import tempfile
        import threading
        import routing_gemini

        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td).resolve()
            pcb = root / "b.kicad_pcb"
            pcb.write_text("(kicad_pcb (version 20241229))\n", encoding="utf-8")
            folder = root / "step"
            folder.mkdir()
            png = folder / "round2-top.png"
            png.write_bytes(b"\x89PNG\r\n\x1a\n")
            clock_now = [1800000000.0]

            prompts: list[str] = []

            def fake_gemini_run(_bin, prompt_text, *_args, **_kw):
                prompts.append(prompt_text)
                if len(prompts) == 1:
                    clock_now[0] = 1800000590.0  # only 10s left (< 30s) -> triggers 90s look extension
                else:
                    clock_now[0] += 15.0
                return True, ""

            calls_round1 = {
                "route": 0, "route_closed": 0, "route_refused": 0, "route_by_status": {},
                "edit": 0, "edit_by_verb": {}, "lay": 1, "lay_laid": 1, "lay_refused": 0, "lay_by_status": {"laid": 1},
                "look": 0, "looked_after_edit": False, "gui_copper_calls": 0, "ipc_copper_calls": 1,
                "wrapper_calls": 0, "other": 0, "other_by_cmd": {}, "repeats": {}, "first_editor_failure": None,
            }
            calls_round2 = {**calls_round1, "look": 1, "looked_after_edit": True}
            done = threading.Event()
            done.set()
            args = SimpleNamespace(
                provider="gemini", review_provider="gemini", model=None, review_model=None,
                effort=None, agent=None, keep_going=False, once=False, minutes=10,
                gemini_binary="agy", codex_binary="codex",
            )
            pcb_sha = editor_step.sha(pcb)
            conns = [{"from": "J1.1", "to": "J2.1", "net": "/SIG1", "before": False, "after": True, "saved_sha256": pcb_sha}]
            with (
                mock.patch.object(editor_step, "ROUTING", {"tasks": True}),
                mock.patch.object(editor_step, "saved_connection", side_effect=[False, True, True]),
                mock.patch.object(editor_step, "layer_pictures", return_value=[png]),
                mock.patch.object(editor_step, "lock"),
                mock.patch.object(editor_step.board_context, "render_board_context", return_value=("", "")),
                mock.patch.object(editor_step, "operator_docs_text", return_value=""),
                mock.patch.object(routing_gemini, "run", side_effect=fake_gemini_run),
                mock.patch.object(
                    routing_gemini,
                    "calls",
                    side_effect=lambda *_a, **_kw: calls_round1 if len(prompts) <= 1 else calls_round2,
                ),
                mock.patch.object(routing_gemini, "tokens", return_value={"rounds": [], "by_model": {}, "complete": True}),
                mock.patch.object(editor_step.time, "time", side_effect=lambda: clock_now[0]),
                mock.patch.object(
                    editor_step,
                    "decide",
                    side_effect=lambda *_a, **_kw: ({"passed": True, "why": "editor_work", "after": {"open": 0, "errors": 0, "findings": 0}, "new_findings": []}, []),
                ),
                mock.patch.object(editor_step, "review", return_value=([], "NOTHING.")),
            ):
                task = "Mode: ipc\nConnection: J1.1 J2.1 /SIG1\n"
                res, _ = editor_step.step_rounds(
                    args, task, pcb, SimpleNamespace(pid=123), "ipc://sock",
                    {"open": 1, "errors": 0, "findings": 0}, [], folder, root, done,
                )

            self.assertEqual(len(prompts), 2, repr(res))
            self.assertTrue(res["passed"], repr(res))
            self.assertEqual(res["why"], "editor_work")
            self.assertEqual(res["rounds"], 2)
            self.assertIn("Do NOT re-route or make copper changes", prompts[1])
            self.assertNotIn("Connection: J1.1 J2.1 /SIG1", prompts[1])

    def test_operator_timeout_preserves_clean_editor_copper_as_candidate(self) -> None:
        import tempfile
        import threading
        import zipfile
        import routing_gemini

        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td).resolve()
            pcb = root / "b.kicad_pcb"
            pcb.write_text("(kicad_pcb (version 20241229))\n", encoding="utf-8")
            updated = '(kicad_pcb (version 20241229)\n\t(segment (start 0 0) (end 5 0) (width 0.25) (layer "F.Cu") (net "/SIG1") (uuid "s1"))\n)\n'
            folder = root / "step"
            folder.mkdir()
            png = folder / "round1-candidate-top.png"
            png.write_bytes(b"\x89PNG\r\n\x1a\n")
            done = threading.Event()
            done.set()
            args = SimpleNamespace(
                provider="gemini", review_provider="gemini", model=None, review_model=None,
                effort=None, agent=None, keep_going=False, once=False, minutes=10,
                gemini_binary="agy", codex_binary="codex",
            )

            def fake_editor_board(_sock, *, save=False, refill=False):
                if save:
                    pcb.write_text(updated, encoding="utf-8")
                return pcb

            def fake_design_check(_p, out_path):
                out_path.write_text("{}", encoding="utf-8")
                return {"open": 1, "errors": 0, "findings": 0}

            with (
                mock.patch.object(editor_step, "ROUTING", {"tasks": True}),
                mock.patch.object(editor_step, "saved_connection", return_value=False),
                mock.patch.object(editor_step, "layer_pictures", return_value=[png]),
                mock.patch.object(editor_step, "lock"),
                mock.patch.object(editor_step, "editor_board", side_effect=fake_editor_board),
                mock.patch.object(editor_step, "design_check", side_effect=fake_design_check),
                mock.patch.object(editor_step, "decide") as decide_mock,
                mock.patch.object(editor_step.board_context, "render_board_context", return_value=("", "")),
                mock.patch.object(editor_step, "operator_docs_text", return_value=""),
                mock.patch.object(routing_gemini, "run", return_value=(False, "Operator did not hand back the editor within the handoff grace period")),
                mock.patch.object(routing_gemini, "calls", return_value=editor_step.calls([])),
                mock.patch.object(routing_gemini, "tokens", return_value={"rounds": [], "by_model": {}, "complete": False}),
            ):
                res, _ = editor_step.step_rounds(
                    args, "Mode: ipc\nConnection: J1.1 J2.1 /SIG1\n", pcb, SimpleNamespace(pid=123), "ipc://sock",
                    {"open": 2, "errors": 0, "findings": 0}, [], folder, root, done,
                )

            decide_mock.assert_not_called()
            self.assertFalse(res["passed"])
            self.assertEqual(res["why"], "operator_failed")
            self.assertEqual(res["after"]["open"], 1)
            self.assertEqual(res["candidate"]["archive"], "candidate-round1.zip")
            with zipfile.ZipFile(folder / "candidate-round1.zip") as bundle:
                self.assertEqual(bundle.read("candidate.kicad_pcb"), pcb.read_bytes())

    def test_operator_timeout_with_new_drc_errors_skips_candidate_archive(self) -> None:
        import tempfile
        import threading
        import routing_gemini

        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td).resolve()
            pcb = root / "b.kicad_pcb"
            pcb.write_text("(kicad_pcb (version 20241229))\n", encoding="utf-8")
            updated = '(kicad_pcb (version 20241229)\n\t(segment (start 0 0) (end 5 0) (width 0.25) (layer "F.Cu") (net "/SIG1") (uuid "s1"))\n)\n'
            folder = root / "step"
            folder.mkdir()
            png = folder / "start-top.png"
            png.write_bytes(b"\x89PNG\r\n\x1a\n")
            done = threading.Event()
            done.set()
            args = SimpleNamespace(
                provider="gemini", review_provider="gemini", model=None, review_model=None,
                effort=None, agent=None, keep_going=False, once=False, minutes=10,
                gemini_binary="agy", codex_binary="codex",
            )

            def fake_editor_board(_sock, *, save=False, refill=False):
                if save:
                    pcb.write_text(updated, encoding="utf-8")
                return pcb

            def fake_design_check(_p, out_path):
                out_path.write_text("{}", encoding="utf-8")
                return {"open": 1, "errors": 1, "findings": 1}

            with (
                mock.patch.object(editor_step, "ROUTING", {"tasks": True}),
                mock.patch.object(editor_step, "saved_connection", return_value=False),
                mock.patch.object(editor_step, "layer_pictures", return_value=[png]),
                mock.patch.object(editor_step, "lock"),
                mock.patch.object(editor_step, "editor_board", side_effect=fake_editor_board),
                mock.patch.object(editor_step, "design_check", side_effect=fake_design_check),
                mock.patch.object(editor_step.board_context, "render_board_context", return_value=("", "")),
                mock.patch.object(editor_step, "operator_docs_text", return_value=""),
                mock.patch.object(routing_gemini, "run", return_value=(False, "Operator did not hand back the editor within the handoff grace period")),
                mock.patch.object(routing_gemini, "calls", return_value=editor_step.calls([])),
                mock.patch.object(routing_gemini, "tokens", return_value={"rounds": [], "by_model": {}, "complete": False}),
            ):
                res, _ = editor_step.step_rounds(
                    args, "Mode: ipc\nConnection: J1.1 J2.1 /SIG1\n", pcb, SimpleNamespace(pid=123), "ipc://sock",
                    {"open": 2, "errors": 0, "findings": 0}, [], folder, root, done,
                )

            self.assertFalse(res["passed"])
            self.assertEqual(res["why"], "operator_failed")
            self.assertEqual(res["after"]["errors"], 1)
            self.assertNotIn("candidate", res)
            self.assertFalse((folder / "candidate-round1.zip").exists())

    def test_review_timeout_preserves_drc_clean_candidate(self) -> None:
        import tempfile
        import threading
        import routing_gemini

        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td).resolve()
            pcb = root / "b.kicad_pcb"
            pcb.write_text("(kicad_pcb (version 20241229))\n", encoding="utf-8")
            updated = '(kicad_pcb (version 20241229)\n\t(segment (start 0 0) (end 5 0) (width 0.25) (layer "F.Cu") (net "/SIG1") (uuid "s1"))\n)\n'
            folder = root / "step"
            folder.mkdir()
            png = folder / "round1-candidate-top.png"
            png.write_bytes(b"\x89PNG\r\n\x1a\n")
            (folder / "drc-end.json").write_text("{}", encoding="utf-8")
            (folder / "quality-round1.json").write_text("{}", encoding="utf-8")
            done = threading.Event()
            done.set()
            args = SimpleNamespace(
                provider="gemini", review_provider="gemini", model=None, review_model=None,
                effort=None, agent=None, keep_going=False, once=False, minutes=10,
                gemini_binary="agy", codex_binary="codex",
            )
            fake_calls = {**editor_step.calls([]), "lay": 1, "lay_laid": 1, "look": 1, "looked_after_edit": True, "ipc_copper_calls": 1}

            def fake_gemini_run(_bin, _prompt, *_args, **kw):
                if kw.get("pid") is not None:
                    pcb.write_text(updated, encoding="utf-8")
                    return True, "done"
                return False, "Independent review timed out"

            with (
                mock.patch.object(editor_step, "ROUTING", {"tasks": True}),
                mock.patch.object(editor_step, "saved_connection", side_effect=[False, True]),
                mock.patch.object(editor_step, "layer_pictures", return_value=[png]),
                mock.patch.object(editor_step, "lock"),
                mock.patch.object(editor_step.board_context, "render_board_context", return_value=("", "")),
                mock.patch.object(editor_step, "operator_docs_text", return_value=""),
                mock.patch.object(routing_gemini, "run", side_effect=fake_gemini_run),
                mock.patch.object(routing_gemini, "calls", return_value=fake_calls),
                mock.patch.object(routing_gemini, "tokens", return_value={"rounds": [], "by_model": {}, "complete": True}),
                mock.patch.object(editor_step, "decide", return_value=({"passed": True, "why": "editor_work", "after": {"open": 0, "errors": 0, "findings": 0}, "new_findings": []}, [])),
            ):
                res, _ = editor_step.step_rounds(
                    args, "Mode: ipc\nConnection: J1.1 J2.1 /SIG1\n", pcb, SimpleNamespace(pid=123), "ipc://sock",
                    {"open": 2, "errors": 0, "findings": 0}, [], folder, root, done,
                )

            self.assertFalse(res["passed"])
            self.assertEqual(res["why"], "review_failed")
            self.assertEqual(res["candidate"]["archive"], "candidate-round1.zip")
            self.assertTrue((folder / "candidate-round1.zip").is_file())

    def test_strictly_improving_progress_earns_multiple_extensions_up_to_cap(self) -> None:
        import os
        import tempfile
        import threading
        import routing_gemini

        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td).resolve()
            pcb = root / "b.kicad_pcb"
            pcb.write_text("(kicad_pcb (version 20241229))\n", encoding="utf-8")
            folder = root / "step"
            folder.mkdir()
            png = folder / "round4-candidate-top.png"
            png.write_bytes(b"\x89PNG\r\n\x1a\n")
            (folder / "drc-end.json").write_text("{}", encoding="utf-8")
            clock_now = [1800000000.0]
            round_idx = [0]

            def fake_gemini_run(_bin, _prompt, *_args, **_kw):
                round_idx[0] += 1
                pcb.write_text(
                    f'(kicad_pcb (version 20241229)\n\t(segment (start 0 0) (end {round_idx[0]} 0) (width 0.25) (layer "F.Cu") (net "/SIG1") (uuid "s{round_idx[0]}"))\n)\n',
                    encoding="utf-8",
                )
                clock_now[0] = (1800000590.0, 1800000880.0, 1800001170.0, 1800001460.0)[round_idx[0] - 1]
                return True, ""

            fake_calls = {**editor_step.calls([]), "lay": 1, "lay_laid": 1, "look": 1, "looked_after_edit": True, "ipc_copper_calls": 1}
            done = threading.Event()
            done.set()
            args = SimpleNamespace(
                provider="gemini", review_provider="gemini", model=None, review_model=None,
                effort=None, agent=None, keep_going=False, once=False, minutes=10,
                gemini_binary="agy", codex_binary="codex",
            )
            decisions = [
                ({"passed": True, "why": "editor_work", "after": {"open": 5 - r, "errors": 0, "findings": 0}, "new_findings": []}, [])
                for r in range(1, 5)
            ]
            with (
                mock.patch.dict(os.environ, {"BOARD_GRAPH_ROUTE_DEADLINE": str(int(1800003600))}),
                mock.patch.object(editor_step, "ROUTING", {"tasks": True}),
                mock.patch.object(editor_step, "saved_connection", side_effect=[False] * 20),
                mock.patch.object(editor_step, "layer_pictures", return_value=[png]),
                mock.patch.object(editor_step, "lock"),
                mock.patch.object(editor_step.board_context, "render_board_context", return_value=("", "")),
                mock.patch.object(editor_step, "operator_docs_text", return_value=""),
                mock.patch.object(routing_gemini, "run", side_effect=fake_gemini_run) as gemini_run,
                mock.patch.object(routing_gemini, "calls", return_value=fake_calls),
                mock.patch.object(routing_gemini, "tokens", return_value={"rounds": [], "by_model": {}, "complete": True}),
                mock.patch.object(editor_step.time, "time", side_effect=lambda: clock_now[0]),
                mock.patch.object(editor_step, "decide", side_effect=decisions),
            ):
                res, _ = editor_step.step_rounds(
                    args, "Mode: ipc\nConnection: J1.1 J2.1 /SIG1\n", pcb, SimpleNamespace(pid=123), "ipc://sock",
                    {"open": 5, "errors": 0, "findings": 0}, [], folder, root, done,
                )

            self.assertEqual(gemini_run.call_count, 4, repr(res))
            self.assertEqual(res["rounds"], 4)
            self.assertEqual(res["why"], "connection_open")
            self.assertEqual(res["candidate"]["archive"], "candidate-round4.zip")
            self.assertTrue((folder / "candidate-round4.zip").is_file())

    def test_editor_step_audit_design_check_and_decide_work_standalone_without_facade(self) -> None:
        import json
        import tempfile
        import editor_step_audit
        import editor_step_kicad

        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td).resolve()
            pcb = root / "b.kicad_pcb"
            pcb.write_text("(kicad_pcb (version 20241229))\n", encoding="utf-8")
            folder = root / "step"
            folder.mkdir()
            out = folder / "drc.json"

            def fake_sub_run(_cmd, **_kw):
                out.write_text(json.dumps({"unconnected_items": [], "violations": []}), encoding="utf-8")
                return SimpleNamespace(returncode=0)

            saved_mod = sys.modules.pop("editor_step", None)
            try:
                with mock.patch.object(editor_step_audit.subprocess, "run", side_effect=fake_sub_run):
                    chk = editor_step_audit.design_check(pcb, out)
                self.assertEqual(chk, {"open": 0, "errors": 0, "findings": 0})

                with (
                    mock.patch.object(editor_step_kicad, "lock"),
                    mock.patch.object(editor_step_kicad, "editor_board", return_value=pcb),
                    mock.patch.object(editor_step_kicad, "same_board", return_value=True),
                    mock.patch.object(editor_step_audit, "design_check", return_value={"open": 0, "errors": 0, "findings": 0}),
                    mock.patch.object(editor_step_audit, "audits", return_value=[]),
                ):
                    res, end_audits = editor_step_audit.decide(
                        pcb, {"open": 1, "errors": 0, "findings": 0}, [], SimpleNamespace(poll=lambda: None),
                        "ipc://sock", [], True, folder, "round1",
                    )
                self.assertTrue(res["passed"])
                self.assertEqual(res["why"], "editor_work")
                self.assertEqual(end_audits, [])
            finally:
                if saved_mod is not None:
                    sys.modules["editor_step"] = saved_mod

    def test_editor_step_script_as_main_registers_facade_and_closes_editor_on_startup_error(self) -> None:
        import subprocess
        import tempfile
        import textwrap

        scripts_dir = pathlib.Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as td:
            probe = textwrap.dedent("""
                import json, pathlib, runpy, sys, types
                sys.path.insert(0, sys.argv[1])
                tmp = pathlib.Path(sys.argv[2])
                pcb = tmp / "b.kicad_pcb"
                pcb.write_text("(kicad_pcb (version 20241229))\\n", encoding="utf-8")
                sys.argv = [str(pathlib.Path(sys.argv[1]) / "editor_step.py"), "--help"]
                try:
                    ns = runpy.run_path(sys.argv[0], run_name="__main__")
                except SystemExit:
                    ns = {}
                import editor_step_audit
                out = tmp / "drc-start.json"
                def fake_run(_cmd, **_kw):
                    out.write_text(json.dumps({"unconnected_items": [], "violations": []}), encoding="utf-8")
                    return types.SimpleNamespace(returncode=0)
                f = editor_step_audit._facade()
                assert "editor_step" in sys.modules, "editor_step must be registered under __main__"
                assert f is sys.modules["editor_step"]
                f.subprocess.run = fake_run
                assert editor_step_audit.design_check(pcb, out) == {"open": 0, "errors": 0, "findings": 0}
                print("OK")
            """).strip()
            done = subprocess.run(
                [sys.executable, "-c", probe, str(scripts_dir), td],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
            self.assertEqual(done.stdout.strip().splitlines()[-1], "OK")

            pcb = pathlib.Path(td) / "b.kicad_pcb"
            pcb.write_text("(kicad_pcb (version 20241229))\n", encoding="utf-8")
            proc = SimpleNamespace(pid=4242)
            with (
                mock.patch.object(sys, "argv", ["editor_step.py", str(pcb), "--task", "Connection: J1.1 J2.1 /SIG1", "--agent", "true"]),
                mock.patch.object(editor_step, "screen_locked", return_value=False),
                mock.patch.object(editor_step, "refuse_if_editor_running"),
                mock.patch.object(editor_step, "open_editor", return_value=(proc, "ipc://sock")),
                mock.patch.object(editor_step, "editor_board"),
                mock.patch.object(editor_step, "design_check", side_effect=RuntimeError("drc boom")),
                mock.patch.object(editor_step, "close_editor") as close_mock,
            ):
                with self.assertRaisesRegex(RuntimeError, "drc boom"):
                    editor_step.main()
            close_mock.assert_called_once_with(proc)


if __name__ == "__main__":
    unittest.main()
