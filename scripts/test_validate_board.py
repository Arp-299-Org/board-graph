#!/usr/bin/env python3
"""The board checks' own logic, without running KiCad: zone fills compared, earlier reports never reused, a git
failure running every check, and added waivers caught."""

from __future__ import annotations

import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
# Fixture board description and machine settings, read at import so every checkout derives the same names.
FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
os.environ["BOARD_GRAPH_BOARD"] = str(FIXTURES / "board.json")
os.environ["BOARD_GRAPH_SITE"] = str(FIXTURES / "board_graph_site.json")
import validate_board  # noqa: E402

BOARD = """(kicad_pcb
\t(version 20260206)
\t(net "Net-(D1-K)")
\t(zone
\t\t(net "Net-(D1-K)")
\t\t(layer "In1.Cu")
\t\t(uuid "e63dad8a")
\t\t(filled_polygon
\t\t\t(layer "In1.Cu")
\t\t\t(pts
\t\t\t\t(xy 0 0) (xy 1 0) (xy 1 1)
\t\t\t)
\t\t)
\t)
)
"""

WAIVER = {"rule": "R5", "match": "U3.6 to C13.2", "reason": "bulk cap distance", "by": "the owner", "date": "2026-09-07"}


def completed(code: int) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, stdout="", stderr="the preflight stopped" if code else "")


class ZoneFills(unittest.TestCase):
    def fills(self, text: str) -> dict:
        with tempfile.TemporaryDirectory() as folder:
            board = pathlib.Path(folder) / "board.kicad_pcb"
            board.write_text(text, encoding="utf-8")
            return validate_board.zone_fills(board)

    def test_a_zone_on_a_net_named_with_parentheses_is_read_whole(self):
        layers, polygons = self.fills(BOARD)["e63dad8a"]
        self.assertEqual(layers, '"In1.Cu"')
        self.assertEqual(len(polygons), 1)
        self.assertEqual(polygons[0].count("(xy "), 3)

    def test_spacing_does_not_count_as_a_difference(self):
        self.assertEqual(self.fills(BOARD), self.fills(BOARD.replace("(xy 0 0) (xy 1 0)", "(xy 0 0)\n\t\t\t\t(xy 1 0)")))

    def test_one_moved_vertex_is_a_difference(self):
        self.assertNotEqual(self.fills(BOARD), self.fills(BOARD.replace("(xy 1 1)", "(xy 1 1.2)")))


class EarlierReports(unittest.TestCase):
    """An audit that fails to run must not pass on a report an earlier run left in --out."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.out = pathlib.Path(self.folder.name)

    def tearDown(self):
        self.folder.cleanup()

    def audit(self, check, report: str, written: dict | None, code: int) -> dict:
        def tool(_script, *args):
            if written is not None:
                target = pathlib.Path(str(args[list(args).index("--json" if "--json" in args else "--output") + 1]))
                target.write_text(json.dumps(written), encoding="utf-8")
            return completed(code)
        (self.out / report).write_text(json.dumps({"pass": True, "status": "pass"}), encoding="utf-8")
        with mock.patch.object(validate_board, "kicad_offline", side_effect=tool):
            return check(self.out)

    def test_electrical_audit_that_did_not_run(self):
        found = self.audit(validate_board.check_electrical, "electrical-audit.json", None, 1)
        self.assertFalse(found["ok"])
        self.assertIn("could not run", found["detail"])

    def test_electrical_audit_that_wrote_a_pass_then_failed(self):
        found = self.audit(validate_board.check_electrical, "electrical-audit.json", {"pass": True}, 1)
        self.assertFalse(found["ok"])
        self.assertIn("could not run", found["detail"])

    def test_electrical_audit_that_found_problems_says_which(self):
        written = {"pass": False, "gated_failures": {"R1": 1}}
        found = self.audit(validate_board.check_electrical, "electrical-audit.json", written, 1)
        self.assertFalse(found["ok"])
        self.assertIn("gated failures", found["detail"])

    def test_electrical_audit_that_passed(self):
        self.assertTrue(self.audit(validate_board.check_electrical, "electrical-audit.json", {"pass": True}, 0)["ok"])

    def test_full_audit_that_did_not_run(self):
        found = self.audit(validate_board.check_exhaustive, "exhaustive-board-audit.json", None, 1)
        self.assertFalse(found["ok"])
        self.assertIn("could not run", found["detail"])

    def test_full_audit_that_wrote_a_pass_then_failed(self):
        found = self.audit(validate_board.check_exhaustive, "exhaustive-board-audit.json", {"status": "pass"}, 1)
        self.assertFalse(found["ok"])

    def test_full_audit_can_pass_without_a_drc_count(self):
        self.assertTrue(self.audit(validate_board.check_exhaustive, "exhaustive-board-audit.json", {"status": "pass"}, 0)["ok"])

    def test_why_a_tool_failed_is_plain_text(self):
        coloured = subprocess.CompletedProcess([], 1, stdout="", stderr="\x1b[31;1mKiCad Python could not import pcbnew\x1b[0m")
        detail = validate_board.tool_failure("Route quality (G3)", coloured, None)["detail"]
        self.assertEqual(detail, "could not run (exit 1): KiCad Python could not import pcbnew")


class ChangedFiles(unittest.TestCase):
    def test_no_base_runs_every_check(self):
        self.assertTrue(validate_board.board_inputs_changed(None))

    def test_a_failed_git_diff_runs_every_check(self):
        with mock.patch.object(validate_board, "run", return_value=completed(128)):
            self.assertTrue(validate_board.board_inputs_changed("0" * 40))

    def test_docs_only_change_skips_the_board_checks(self):
        diff = subprocess.CompletedProcess([], 0, stdout="docs/ROADMAP.md\0README.md\0", stderr="")
        with mock.patch.object(validate_board, "run", return_value=diff):
            self.assertFalse(validate_board.board_inputs_changed("0" * 40))

    def test_a_board_change_runs_the_board_checks(self):
        diff = subprocess.CompletedProcess([], 0, stdout="docs/ROADMAP.md\0board/demo.kicad_pcb\0", stderr="")
        with mock.patch.object(validate_board, "run", return_value=diff):
            self.assertTrue(validate_board.board_inputs_changed("0" * 40))

    def test_a_change_to_the_preflights_console_helper_runs_the_board_checks(self):
        diff = subprocess.CompletedProcess([], 0, stdout="scripts/invoke_console.ps1\0", stderr="")
        with mock.patch.object(validate_board, "run", return_value=diff):
            self.assertTrue(validate_board.board_inputs_changed("0" * 40))


class Inherited(unittest.TestCase):
    def test_a_failure_the_base_shares_word_for_word_is_inherited(self):
        results = [{"check": "DRC", "ok": False, "detail": "70 mismatches"}]
        validate_board.mark_inherited(results, {"DRC": "70 mismatches"})
        self.assertTrue(results[0]["ok"])
        self.assertTrue(results[0]["inherited"])
        self.assertIn("70 mismatches", results[0]["detail"])

    def test_one_more_mismatch_than_the_base_still_fails(self):
        results = [{"check": "DRC", "ok": False, "detail": "71 mismatches"}]
        validate_board.mark_inherited(results, {"DRC": "70 mismatches"})
        self.assertFalse(results[0]["ok"])

    def test_a_check_only_this_change_fails_still_fails(self):
        results = [{"check": "DRC", "ok": False, "detail": "1 clearance"}]
        validate_board.mark_inherited(results, {})
        self.assertFalse(results[0]["ok"])

    def test_a_base_that_cannot_be_named_knows_nothing(self):
        self.assertEqual(validate_board.base_failures(None, pathlib.Path("unused")), ({}, None))


class BaseOpenCount(unittest.TestCase):
    def count(self, checks, drc_report):
        summary = {"checks": checks}
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(validate_board, "run", return_value=completed(0)), \
                mock.patch.object(validate_board.subprocess, "run", return_value=completed(0)), \
                mock.patch.object(validate_board, "load", side_effect=[summary, drc_report]):
            return validate_board.base_failures("base", pathlib.Path(folder))[1]

    def test_base_report_without_a_usable_count_is_unknown(self):
        checks = [{"check": "DRC", "ok": True, "detail": "pass", "open": 0}]
        for report in (None, {}, {"unconnected_items": None}, {"unconnected_items": ""}):
            with self.subTest(report=report):
                self.assertIsNone(self.count(checks, report))

    def test_a_failed_base_drc_cannot_supply_a_count_from_a_report(self):
        checks = [{"check": "DRC", "ok": False, "detail": "could not run"}]
        self.assertIsNone(self.count(checks, {"unconnected_items": []}))

    def test_measured_base_counts_remain_available(self):
        for items in ([], [1, 2, 3]):
            with self.subTest(items=items):
                checks = [{"check": "DRC", "ok": True, "detail": "pass", "open": len(items)}]
                self.assertEqual(self.count(checks, {"unconnected_items": items}), len(items))

    def test_disagreeing_base_summary_and_report_do_not_supply_a_count(self):
        checks = [{"check": "DRC", "ok": True, "detail": "pass", "open": 0}]
        self.assertIsNone(self.count(checks, {"unconnected_items": [1]}))


class Waivers(unittest.TestCase):
    def text(self, *waivers: dict) -> str:
        return json.dumps({"waivers": list(waivers)})

    def test_unchanged_waivers(self):
        self.assertEqual(validate_board.added_waivers(self.text(WAIVER), self.text(WAIVER)), [])

    def test_a_removed_waiver_is_fine(self):
        self.assertEqual(validate_board.added_waivers(self.text(WAIVER), self.text()), [])

    def test_a_new_waiver_is_caught(self):
        new = {**WAIVER, "match": "SD1.4 to C12.1"}
        self.assertEqual(validate_board.added_waivers(self.text(WAIVER), self.text(WAIVER, new)), [new])

    def test_a_broadened_waiver_is_caught(self):
        broader = {**WAIVER, "match": "U3.6"}
        self.assertEqual(validate_board.added_waivers(self.text(WAIVER), self.text(broader)), [broader])

    def test_a_waiver_file_new_in_this_change_is_all_added(self):
        self.assertEqual(validate_board.added_waivers(None, self.text(WAIVER)), [WAIVER])

    def test_no_base_found_fails_the_check(self):
        self.assertFalse(validate_board.check_waivers(None)["ok"])


class CopperOrigin(unittest.TestCase):
    """The change's copper came through the routing run, or the row fails and says what to do."""

    def check(self, found=(), error=None):
        import board_copper_origin
        with mock.patch.object(board_copper_origin, "unaccounted", side_effect=error,
                               return_value=[{"words": line} for line in found]) as asked:
            row = validate_board.check_copper_origin("origin/main")
        return row, asked

    def test_accounted_copper_passes_and_only_the_committed_board_is_read(self):
        row, asked = self.check()
        self.assertTrue(row["ok"])
        self.assertEqual(row["check"], "Copper origin")
        self.assertEqual(asked.call_args.args[3], "origin/main")
        self.assertIs(asked.call_args.kwargs["worktree"], False)

    def test_copper_from_outside_the_run_fails_and_names_the_way_out(self):
        import board_copper_origin
        row, _ = self.check([f"{n} tracks added outside the routing run, by commit c{n}" for n in range(8)])
        self.assertFalse(row["ok"])
        self.assertIn("0 tracks added outside the routing run, by commit c0; ", row["detail"])
        self.assertIn("; and 2 more. ", row["detail"])
        self.assertTrue(row["detail"].endswith(board_copper_origin.WAY_OUT))

    def test_a_board_that_cannot_be_read_fails(self):
        import board_copper_origin
        row, _ = self.check(error=board_copper_origin.Unreadable("git log: bad object"))
        self.assertFalse(row["ok"])
        self.assertIn("could not be read: git log: bad object", row["detail"])


class NotRoutedYetIsProgress(unittest.TestCase):
    INF = "U3.14 to C10.2: inf mm of track (> 3.0)"
    REAL = "R32 is 14.2 mm from U8.9 (> 5.0)"
    PAD = "Pad D3.1 on GND does not connect to another pad"

    def electrical(self, findings, open_count):
        audit = {"pass": False, "findings": findings, "gated_failures": {"R5": 1}}
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(validate_board, "kicad_offline", return_value=completed(1)), \
                mock.patch.object(validate_board, "load", return_value=audit):
            return validate_board.check_electrical(pathlib.Path(folder), open_count)

    def full(self, errors, open_count):
        audit = {"status": "fail", "errors": errors}
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(validate_board, "kicad_offline", return_value=completed(1)), \
                mock.patch.object(validate_board, "load", return_value=audit):
            return validate_board.check_exhaustive(pathlib.Path(folder), open_count)

    def test_an_infinite_length_waits_on_routing_while_nets_are_open(self):
        self.assertTrue(self.electrical({"R5": [self.INF]}, 155)["ok"])

    def test_a_measured_finding_is_listed_without_failing_on_an_unrouted_board(self):
        made = self.electrical({"R5": [self.INF], "R7": [self.REAL]}, 155)
        self.assertTrue(made["ok"])
        self.assertIn("R32", made["detail"])
        self.assertNotIn("inf mm", made["detail"])

    def test_a_measured_finding_fails_once_nothing_is_open(self):
        made = self.electrical({"R7": [self.REAL]}, 0)
        self.assertFalse(made["ok"])
        self.assertIn("R32", made["detail"])

    def test_an_infinite_length_fails_once_nothing_is_open(self):
        self.assertFalse(self.electrical({"R5": [self.INF]}, 0)["ok"])

    def test_an_unknown_open_count_does_not_defer_electrical_findings(self):
        made = self.electrical({"R5": [self.INF]}, None)
        self.assertFalse(made["ok"])
        self.assertIn(self.INF, made["detail"])

    def test_a_failed_gate_with_no_finding_still_fails_on_an_unrouted_board(self):
        self.assertFalse(self.electrical({}, 155)["ok"])

    def test_empty_pads_wait_on_routing_while_nets_are_open(self):
        self.assertTrue(self.full([self.PAD], 155)["ok"])

    def test_empty_pads_fail_once_nothing_is_open(self):
        self.assertFalse(self.full([self.PAD], 0)["ok"])

    def test_an_unknown_open_count_does_not_defer_empty_pads(self):
        made = self.full([self.PAD], None)
        self.assertFalse(made["ok"])
        self.assertIn(self.PAD, made["detail"])

    def test_any_other_audit_error_still_fails_on_an_unrouted_board(self):
        made = self.full([self.PAD, "J2 is on the wrong side"], 155)
        self.assertFalse(made["ok"])
        self.assertIn("wrong side", made["detail"])

    def test_fewer_open_passes(self):
        self.assertTrue(validate_board.check_progress(100, 155, 0)["ok"])

    def test_no_base_passes_and_says_the_count(self):
        made = validate_board.check_progress(155, None, 0)
        self.assertTrue(made["ok"])
        self.assertIn("155 open", made["detail"])

    def test_unknown_current_count_fails_even_if_parts_moved(self):
        made = validate_board.check_progress(None, 155, 1)
        self.assertFalse(made["ok"])
        self.assertIn("did not provide", made["detail"])
        self.assertNotIn("none open", made["detail"])

    def test_measured_zero_still_passes(self):
        made = validate_board.check_progress(0, 155, 0)
        self.assertTrue(made["ok"])
        self.assertIn("none open", made["detail"])

    def test_more_open_with_no_part_moved_fails(self):
        self.assertFalse(validate_board.check_progress(156, 155, 0)["ok"])

    def test_more_open_with_parts_moved_passes(self):
        self.assertTrue(validate_board.check_progress(155, 0, 40)["ok"])

    def test_placements_reads_reference_and_position(self):
        text = '  (footprint "lib:R"\n    (layer "B.Cu")\n    (at 1 2 90)\n    (property "Reference" "R1"\n      (at 9 9 0))\n  )\n'
        self.assertEqual(validate_board.placements(text), {"R1": "1 2 90"})


class ManualJoints(unittest.TestCase):
    def run_with(self, rows, counts, passed):
        import board_manual_joints
        rep = {"rows": rows, "counts": counts, "pass": passed}
        with tempfile.TemporaryDirectory() as folder,                 mock.patch.object(board_manual_joints, "report", return_value=rep):
            made = validate_board.check_manual_joints(pathlib.Path(folder))
            self.assertTrue((pathlib.Path(folder) / "manual-joints.json").is_file())
            return made

    ROW = {"part": "the controller module (U8)", "pads": ["1"], "verdict": "NOT PROVEN", "mask_ok": True, "solid": False,
           "mask_faces": ["F", "B"], "pin": "not on record", "hole_finished_min": "round 1.02", "margin_mm": None,
           "connection": "no plane on its net"}

    def test_not_proven_passes_and_is_named(self):
        made = self.run_with([self.ROW], {"rows": 1, "not_proven": 1}, True)
        self.assertTrue(made["ok"])
        self.assertIn("1 not proven (the controller module (U8))", made["detail"])

    def test_a_pin_that_does_not_fit_fails_and_says_which(self):
        row = {**self.ROW, "part": "the display header (J2)", "verdict": "DOES NOT FIT", "pin_verdict": "DOES NOT FIT", "pin": "0.64 x 0.64",
               "hole_finished_min": "round 0.67", "margin_mm": -0.235}
        made = self.run_with([row], {"rows": 1, "not_proven": 0}, False)
        self.assertFalse(made["ok"])
        self.assertIn("the display header (J2) pads 1", made["detail"])

    def test_a_board_that_does_not_read_fails(self):
        import board_manual_joints
        with tempfile.TemporaryDirectory() as folder,                 mock.patch.object(board_manual_joints, "report", side_effect=ValueError("unterminated list")):
            made = validate_board.check_manual_joints(pathlib.Path(folder))
        self.assertFalse(made["ok"])
        self.assertIn("unterminated list", made["detail"])


class DrcCountsOpenWithoutFailing(unittest.TestCase):
    def run_with(self, report):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(validate_board, "drc", return_value=(completed(0), pathlib.Path(folder) / "d.json")), \
                mock.patch.object(validate_board, "load", return_value=report), \
                mock.patch.object(validate_board, "DOCUMENTED_DRC", mock.Mock(read_text=lambda: json.dumps({"accepted_warning_refs": []}))):
            return validate_board.check_drc(pathlib.Path(folder))[1]

    def test_unconnected_is_counted_not_failed(self):
        made = self.run_with({"violations": [], "unconnected_items": [1, 2, 3], "schematic_parity": []})
        self.assertTrue(made["ok"])
        self.assertEqual(made["open"], 3)

    def test_empty_unconnected_list_is_a_measured_zero(self):
        made = self.run_with({"violations": [], "unconnected_items": [], "schematic_parity": []})
        self.assertTrue(made["ok"])
        self.assertEqual(made["open"], 0)

    def test_missing_or_unusable_unconnected_list_is_unknown(self):
        for invalid in ({}, {"unconnected_items": None}, {"unconnected_items": ""},
                        {"unconnected_items": {}}, {"unconnected_items": 0}):
            with self.subTest(report=invalid):
                made = self.run_with({"violations": [], "schematic_parity": [], **invalid})
                self.assertFalse(made["ok"])
                self.assertIsNone(made["open"])
                self.assertIn("no usable unconnected-items list", made["detail"])

    def test_a_clearance_error_still_fails(self):
        made = self.run_with({"violations": [{"type": "clearance"}], "unconnected_items": [1], "schematic_parity": []})
        self.assertFalse(made["ok"])

    def test_a_schematic_mismatch_still_fails(self):
        made = self.run_with({"violations": [], "unconnected_items": [1], "schematic_parity": [1]})
        self.assertFalse(made["ok"])


class DrcCountIntegration(unittest.TestCase):
    def run_checks(self, code, report, with_base=False, expected_count=None):
        with tempfile.TemporaryDirectory() as folder:
            out = pathlib.Path(folder).resolve()
            drc_report = out / "drc.json"
            if report is not None:
                drc_report.write_text(json.dumps(report), encoding="utf-8")
            ok = validate_board.result("other check", True, "pass")
            inherited = {"KiCad version": "DRC did not run",
                         "Board rules check, DRC (G2), zones refilled":
                             "could not run (exit 1): the preflight stopped"}
            args = ["validate_board.py", "--out", str(out)]
            if with_base:
                args += ["--base", "origin/main"]
            with mock.patch.object(sys, "argv", args), \
                    mock.patch("sys.stdout", new_callable=io.StringIO), \
                    mock.patch.object(validate_board, "drc", return_value=(completed(code), drc_report)), \
                    mock.patch.object(validate_board, "DOCUMENTED_DRC", mock.Mock(read_text=lambda: json.dumps({"accepted_warning_refs": []}))), \
                    mock.patch.object(validate_board, "check_erc", return_value=ok), \
                    mock.patch.object(validate_board, "check_saved_fills", return_value=ok), \
                    mock.patch.object(validate_board, "check_route_quality", return_value=ok), \
                    mock.patch.object(validate_board, "check_pad_mask", return_value=ok), \
                    mock.patch.object(validate_board, "check_thermal_relief", return_value=ok),                     mock.patch.object(validate_board, "check_manual_joints", return_value=ok), \
                    mock.patch.object(validate_board, "check_electrical", return_value=ok) as electrical, \
                    mock.patch.object(validate_board, "check_exhaustive", return_value=ok) as exhaustive, \
                    mock.patch.object(validate_board, "merge_base", return_value="base"), \
                    mock.patch.object(validate_board, "board_inputs_changed", return_value=True), \
                    mock.patch.object(validate_board, "check_waivers", return_value=ok), \
                    mock.patch.object(validate_board, "check_copper_origin", return_value=ok), \
                    mock.patch.object(validate_board, "base_failures", return_value=(inherited, None)):
                exit_code = validate_board.main()
            electrical.assert_called_once_with(out, expected_count)
            exhaustive.assert_called_once_with(out, expected_count)
            summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
            progress = next(item for item in summary["checks"] if item["check"] == "Routing progress")
            if expected_count is None:
                self.assertEqual(exit_code, 1)
                self.assertFalse(summary["pass"])
                self.assertFalse(progress["ok"])
                self.assertIn("did not provide", progress["detail"])
                self.assertNotIn("none open", progress["detail"])
            else:
                self.assertEqual(exit_code, 0)
                self.assertTrue(summary["pass"])
                self.assertTrue(progress["ok"])
                self.assertIn("none open" if expected_count == 0 else f"{expected_count} open", progress["detail"])
            return summary

    def test_failed_drc_with_a_report_does_not_use_the_report_count(self):
        self.run_checks(1, {"unconnected_items": []})

    def test_missing_report_keeps_the_count_unknown_even_on_exit_zero(self):
        self.run_checks(0, None)

    def test_report_without_a_count_keeps_the_count_unknown(self):
        self.run_checks(0, {"violations": [], "schematic_parity": []})

    def test_inherited_drc_failure_cannot_make_unknown_progress_pass(self):
        summary = self.run_checks(1, None, with_base=True)
        drc = next(item for item in summary["checks"] if item["check"].startswith("Board rules check"))
        self.assertTrue(drc["inherited"])

    def test_valid_zero_and_nonzero_counts_reach_progress(self):
        for items in ([], [1, 2, 3]):
            with self.subTest(items=items):
                self.run_checks(0, {"violations": [], "unconnected_items": items, "schematic_parity": []},
                                expected_count=len(items))


class AcceptedRouteQualityWarnings(unittest.TestCase):
    """G3 accepts route-quality warnings only when each warning's exact geometry matches a verified FINE AS IS
    verdict in an adopted routing receipt for the same board."""

    SHA_A = "a" * 64
    SHA_B = "b" * 64

    @staticmethod
    def finding(net, point, message, rule="micro_segment", severity="warning", layer=0):
        return {"severity": severity, "rule": rule, "net": net, "layer": layer, "point_mm": point, "message": message}

    @staticmethod
    def write_receipt(path, pcb_rel, warnings, sha, *, adopted=True, answer=None, task="01-net"):
        path.parent.mkdir(parents=True, exist_ok=True)
        labels = [w.split(",", 1)[0] for w in warnings]
        if answer is None:
            answer = "\n".join(f"FINE AS IS: Warning {i}: clean fine-pitch branch." for i in range(1, len(warnings) + 1))
        step = {
            "task": task,
            "kept": True,
            "why": "warning_accepted",
            "reviews": 1,
            "review_open": [],
            "provider": "codex",
            "model": "gpt-6.1-terra",
            "review_provider": "codex",
            "review_model": "gpt-6.1-terra",
            "new_findings": labels,
            "matching_geometry": list(warnings),
            "reviewed_sha256": sha,
            "pad_exits": [],
            "review_receipt": {
                "schema": "board-graph/routing-review/v1",
                "board_sha256": sha,
                "reviewer": "gpt-6.1-terra",
                "answer": answer,
                "earlier": [],
                "warnings": list(warnings),
            },
        }
        summary = {"summary": True, "adopted": adopted, "kept": 1, "tasks": 1, "provider": "codex", "pcb": pcb_rel}
        path.write_text("\n".join(json.dumps(row) for row in (step, summary)) + "\n", encoding="utf-8")

    def run_g3(self, root, report_data):
        out = root / "out"
        out.mkdir(parents=True, exist_ok=True)

        def tool(_script, *args):
            target = pathlib.Path(str(args[list(args).index("--output") + 1]))
            target.write_text(json.dumps(report_data), encoding="utf-8")
            return completed(0)

        with mock.patch.object(validate_board, "ROOT", root), \
                mock.patch.object(validate_board, "kicad_offline", side_effect=tool):
            return validate_board.check_route_quality(out)

    def test_warnings_accepted_across_separate_adopted_receipts_pass_g3(self):
        pcb_rel = validate_board.DESCRIPTION.rel("pcb")
        w1 = self.finding("/MOTOR_A", [10.0, 20.0], "0.1200 mm: (10.0, 20.0) -> (10.085, 20.085)")
        w2 = self.finding("/MOTOR_B", [12.0, 20.0], "0.1100 mm: (12.0, 20.0) -> (12.078, 20.078)")
        d1 = validate_board.routing_evidence.route_quality_warning_detail(w1)
        d2 = validate_board.routing_evidence.route_quality_warning_detail(w2)
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            routing_dir = (root / pcb_rel).parent / "routing"
            self.write_receipt(routing_dir / "run-11111111.jsonl", pcb_rel, [d1], self.SHA_A, task="01-a")
            self.write_receipt(routing_dir / "run-22222222.jsonl", pcb_rel, [d2], self.SHA_B, task="02-b")
            report = {"summary": {"errors": 0, "warnings": 2, "tracks": 10, "vias": 0}, "findings": [w1, w2]}
            checked = self.run_g3(root, report)
        self.assertTrue(checked["ok"])
        self.assertIn("all 2 reviewer-accepted", checked["detail"])

    def test_unreviewed_changed_unadopted_or_error_findings_still_fail_g3(self):
        pcb_rel = validate_board.DESCRIPTION.rel("pcb")
        w1 = self.finding("/MOTOR_A", [10.0, 20.0], "0.1200 mm: (10.0, 20.0) -> (10.085, 20.085)")
        moved = self.finding("/MOTOR_A", [10.0, 20.0], "0.1210 mm: (10.0, 20.0) -> (10.086, 20.086)")
        w2 = self.finding("/MOTOR_B", [12.0, 20.0], "0.1100 mm: (12.0, 20.0) -> (12.078, 20.078)")
        d1 = validate_board.routing_evidence.route_quality_warning_detail(w1)
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            routing_dir = (root / pcb_rel).parent / "routing"
            # Unadopted receipt does not count.
            self.write_receipt(routing_dir / "run-unadopted.jsonl", pcb_rel, [d1], self.SHA_A, adopted=False)
            self.assertFalse(self.run_g3(root, {"summary": {"errors": 0, "warnings": 1, "tracks": 4, "vias": 0},
                                                "findings": [w1]})["ok"])
            # Adopted receipt covers w1, not moved geometry, not extra unreviewed w2, and not errors.
            self.write_receipt(routing_dir / "run-11111111.jsonl", pcb_rel, [d1], self.SHA_A)
            self.assertFalse(self.run_g3(root, {"summary": {"errors": 0, "warnings": 1, "tracks": 4, "vias": 0},
                                                "findings": [moved]})["ok"])
            mixed = self.run_g3(root, {"summary": {"errors": 0, "warnings": 2, "tracks": 4, "vias": 0},
                                       "findings": [w1, w2]})
            self.assertFalse(mixed["ok"])
            self.assertIn("1 unreviewed, 1 reviewer-accepted", mixed["detail"])
            self.assertFalse(self.run_g3(root, {"summary": {"errors": 1, "warnings": 1, "tracks": 4, "vias": 0},
                                                "findings": [w1, self.finding("/MOTOR_A", [0, 0], "90", "hard_90_corner", "error")]})["ok"])


class SpeedupPreflightAndZeroZone(unittest.TestCase):
    def test_check_saved_fills_skips_refill_drc_on_zero_zone_board(self):
        with tempfile.TemporaryDirectory() as td:
            out = pathlib.Path(td)
            with mock.patch.object(validate_board, "zone_fills", return_value={}), \
                    mock.patch.object(validate_board, "run_kicad_cli", side_effect=AssertionError("should not run")) as cli:
                row = validate_board.check_saved_fills(out)
            self.assertTrue(row["ok"])
            self.assertIn("0 zones", row["detail"])
            cli.assert_not_called()

    def test_kicad_offline_rejects_unregistered_and_caches_version_probe(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            proj = root / "board"
            proj.mkdir()
            (proj / "demo.kicad_pro").write_text("{}", encoding="utf-8")
            (proj / "demo.kicad_pcb").write_text("(kicad_pcb)", encoding="utf-8")
            (proj / "demo.kicad_sch").write_text("(kicad_sch)", encoding="utf-8")
            fake_py = root / "python.exe"
            fake_py.write_text("", encoding="utf-8")
            calls = []

            def fake_run(cmd):
                calls.append(cmd)
                if len(cmd) >= 2 and cmd[1] == "-c":
                    return subprocess.CompletedProcess(cmd, 0, stdout="3.12.0|10.0.8\n", stderr="")
                return subprocess.CompletedProcess(cmd, 0, stdout='{"ok": true}\n', stderr="")

            with mock.patch.object(validate_board, "ROOT", root), \
                    mock.patch.object(validate_board, "PROJECT", "board"), \
                    mock.patch.object(validate_board, "_PREFLIGHT_CACHE", set()), \
                    mock.patch.object(validate_board, "_check_windows_remote_guard"), \
                    mock.patch.object(validate_board, "run", side_effect=fake_run), \
                    mock.patch.dict(os.environ, {"KICAD_PYTHON": str(fake_py)}, clear=False):
                missing = validate_board.kicad_offline("scripts/not_registered_xyz.py")
                self.assertEqual(missing.returncode, 1)
                self.assertIn("does not exist", missing.stderr)
                bad = validate_board.kicad_offline("scripts/board_graph.py")
                self.assertEqual(bad.returncode, 1)
                self.assertIn("not reviewed", bad.stderr)
                r1 = validate_board.kicad_offline("scripts/kicad_report_pad_mask.py", "--board", "x")
                r2 = validate_board.kicad_offline("scripts/kicad_report_thermal_relief.py", "--board", "x")
            self.assertEqual(r1.returncode, 0)
            self.assertEqual(r2.returncode, 0)
            probe_calls = [c for c in calls if len(c) >= 2 and c[1] == "-c"]
            self.assertEqual(len(probe_calls), 1)


if __name__ == "__main__":
    unittest.main()
