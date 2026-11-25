#!/usr/bin/env python3
"""The hand-soldered joint check on small boards built here: a pin that fits, a pin that does not fit, a slot, a
pad with no mask on one face, a solid setting, and a part with no pin on record."""

from __future__ import annotations

import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
# Fixture board description and machine settings, read at import so every checkout derives the same names.
FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
os.environ["BOARD_GRAPH_BOARD"] = str(FIXTURES / "board.json")
os.environ["BOARD_GRAPH_SITE"] = str(FIXTURES / "board_graph_site.json")
import board_manual_joints as joints  # noqa: E402

SQUARE_PIN = {"shape": "rect", "width_mm": 0.64, "thickness_mm": 0.64, "where": "a header drawing p. 1"}


def pad(number, drill, size=(1.7, 1.7), layers='"*.Cu" "*.Mask"', net="SIG", extra=""):
    return (f'\t\t(pad "{number}" thru_hole circle\n\t\t\t(at 0 0)\n\t\t\t(size {size[0]} {size[1]})\n'
            f'\t\t\t(drill {drill})\n\t\t\t(layers {layers})\n\t\t\t(net "{net}")\n{extra}\t\t)\n')


def footprint(ref, mpn, *pads, extra=""):
    return (f'\t(footprint "lib:{ref}"\n\t\t(layer "F.Cu")\n\t\t(at 10 10)\n'
            f'\t\t(property "Reference" "{ref}")\n\t\t(property "Value" "part")\n'
            f'\t\t(property "Manufacturer Part" "{mpn}")\n{extra}' + "".join(pads) + "\t)\n")


GND_PLANE = ('\t(zone\n\t\t(net "GND")\n\t\t(layer "In1.Cu")\n\t\t(uuid "z1")\n'
             '\t\t(connect_pads\n\t\t\t(clearance 0.2)\n\t\t)\n\t)\n')


class Joints(unittest.TestCase):
    def run_on(self, footprints, parts, zones="", issues=()):
        with tempfile.TemporaryDirectory() as folder:
            board = pathlib.Path(folder) / "b.kicad_pcb"
            board.write_text("(kicad_pcb\n\t(version 20260206)\n\t(general\n\t\t(thickness 1.6)\n\t)\n"
                             + footprints + zones + ")\n", encoding="utf-8")
            sheet = pathlib.Path(folder) / "parts.json"
            sheet.write_text(json.dumps({"parts": parts}), encoding="utf-8")
            graph = pathlib.Path(folder) / "graph.json"
            graph.write_text(json.dumps({"open_issues": list(issues)}), encoding="utf-8")
            return joints.report(board, sheet, graph)

    def one(self, rep):
        self.assertEqual(len(rep["rows"]), 1)
        return rep["rows"][0]

    def test_an_ordinary_square_pin_fits_a_1_1_mm_hole(self):
        row = self.one(self.run_on(footprint("J6", "HDR", pad("1", 1.1)), {"HDR": {"pins": [SQUARE_PIN]}}))
        self.assertEqual(row["verdict"], joints.FITS)
        # 1.10 - 0.08 = 1.02 finished, against 0.905 corner to corner
        self.assertAlmostEqual(row["margin_mm"], 1.02 - 0.905, places=3)
        self.assertEqual(row["hole_finished_min"], "round 1.02")

    def test_a_square_pin_in_a_0_75_mm_hole_does_not_fit(self):
        rep = self.run_on(footprint("J2", "HDR", pad("1", 0.75)), {"HDR": {"pins": [SQUARE_PIN]}})
        row = self.one(rep)
        self.assertEqual(row["verdict"], joints.DOES_NOT_FIT)
        self.assertLess(row["margin_mm"], 0)
        self.assertFalse(rep["pass"])
        self.assertIn("does not fit", joints.problems(rep)[0])

    def test_a_flat_pin_in_a_slot_is_judged_both_ways(self):
        slot = pad("1", "oval 1 2", size=(1.5, 2.5))
        pin = {"shape": "rect", "width_mm": 1.2, "width_tol_mm": 0.1, "thickness_mm": 0.3, "where": "p. 2"}
        row = self.one(self.run_on(footprint("J5", "JACK", slot), {"JACK": {"pins": [pin]}}))
        self.assertEqual(row["hole"], "slot 1.00 x 2.00")
        self.assertEqual(row["hole_finished_min"], "slot 0.92 x 1.92")
        self.assertEqual(row["verdict"], joints.FITS)
        self.assertAlmostEqual(row["margin_mm"], 0.57, places=2)
        self.assertAlmostEqual(row["least_ring_mm"], (1.5 - 1.13) / 2, places=3)

    def test_a_pin_wider_than_the_slot_does_not_fit(self):
        slot = pad("1", "oval 0.6 2", size=(1.2, 2.5))
        pin = {"shape": "rect", "width_mm": 1.2, "thickness_mm": 0.6, "where": "p. 2"}
        row = self.one(self.run_on(footprint("J5", "JACK", slot), {"JACK": {"pins": [pin]}}))
        self.assertEqual(row["verdict"], joints.DOES_NOT_FIT)

    def test_a_pad_with_no_mask_on_one_face_fails(self):
        covered = pad("1", 1.1, layers='"*.Cu" "F.Mask"')
        rep = self.run_on(footprint("J6", "HDR", covered), {"HDR": {"pins": [SQUARE_PIN]}})
        row = self.one(rep)
        self.assertFalse(row["mask_ok"])
        self.assertFalse(rep["pass"])
        self.assertIn("no mask opening on B", joints.problems(rep)[0])

    def test_a_solid_pad_override_onto_a_plane_fails(self):
        solid = pad("1", 1.1, net="GND", extra="\t\t\t(zone_connect 2)\n")
        rep = self.run_on(footprint("U8", "HDR", solid), {"HDR": {"pins": [SQUARE_PIN]}}, GND_PLANE)
        row = self.one(rep)
        self.assertTrue(row["solid"])
        self.assertIn("pad setting", row["connection"])
        self.assertFalse(rep["pass"])

    def test_a_solid_footprint_override_and_a_solid_plane_fail(self):
        on_gnd = pad("1", 1.1, net="GND")
        rep = self.run_on(footprint("U8", "HDR", on_gnd, extra="\t\t(zone_connect 2)\n"),
                          {"HDR": {"pins": [SQUARE_PIN]}}, GND_PLANE)
        self.assertIn("footprint setting", self.one(rep)["connection"])
        self.assertFalse(rep["pass"])
        solid_plane = GND_PLANE.replace("(connect_pads\n", "(connect_pads yes\n")
        rep = self.run_on(footprint("U8", "HDR", on_gnd), {"HDR": {"pins": [SQUARE_PIN]}}, solid_plane)
        self.assertTrue(self.one(rep)["solid"])

    def test_a_plane_default_is_thermal(self):
        rep = self.run_on(footprint("U8", "HDR", pad("1", 1.1, net="GND")), {"HDR": {"pins": [SQUARE_PIN]}},
                          GND_PLANE)
        self.assertEqual(self.one(rep)["connection"], "thermal (plane setting)")
        self.assertTrue(rep["pass"])

    def test_a_part_with_no_pin_on_record_is_not_proven_and_does_not_fail(self):
        rep = self.run_on(footprint("U8", "MOD-1", pad("1", 1.1)),
                          {"MOD-1": {"pins": [{"shape": None, "where": "all pages searched"}]}})
        row = self.one(rep)
        self.assertEqual(row["verdict"], joints.NOT_PROVEN)
        self.assertTrue(rep["pass"])
        self.assertEqual(rep["counts"]["not_proven"], 1)
        self.assertIn("all pages searched", joints.unproven(rep)[0])

    def test_a_part_missing_from_parts_json_is_not_proven(self):
        row = self.one(self.run_on(footprint("X1", "UNLISTED", pad("1", 1.1)), {}))
        self.assertEqual(row["verdict"], joints.NOT_PROVEN)
        self.assertIn("not in parts.json", row["pin_where"])

    def test_same_joints_share_a_row_and_unplated_holes_are_left_out(self):
        unplated = '\t\t(pad "" np_thru_hole circle\n\t\t\t(at 1 1)\n\t\t\t(size 1.8 1.8)\n\t\t\t(drill 1.8)\n\t\t\t(layers "*.Cu" "*.Mask")\n\t\t)\n'
        row = self.one(self.run_on(footprint("J6", "HDR", pad("1", 1.1), pad("2", 1.1), unplated),
                                   {"HDR": {"pins": [SQUARE_PIN]}}))
        self.assertEqual(row["pads"], ["1", "2"])
        self.assertEqual(row["holes"], 2)

    TRIM_PEG = {"shape": "rect", "width_mm": 2.0, "thickness_mm": None, "length_mm": 0.9, "where": "p. 390"}
    TRIM_HOLE = {"shape": "rect", "x_mm": 1.0, "y_mm": 2.2, "where": "p. 390"}

    def trim_part(self, drill, peg=None):
        slot = pad("7", drill, size=(1.6, 3.2))
        return self.run_on(footprint("R14", "TRIM", slot),
                           {"TRIM": {"pins": [peg or self.TRIM_PEG], "recommended_hole": self.TRIM_HOLE}})

    def test_a_hole_drawn_at_least_the_makers_is_as_the_maker_draws_it(self):
        rep = self.trim_part("oval 1 2.5")
        row = self.one(rep)
        self.assertEqual(row["verdict"], joints.AS_MAKER)
        self.assertTrue(rep["pass"])
        self.assertEqual(joints.unproven(rep), [])
        # the fab's tolerance can take the drawn 1.00 under the maker's 1.00: the shortfall is printed
        self.assertAlmostEqual(row["against_recommended"]["finished_short_mm"], -0.08)
        self.assertIn("finished -0.08 / +0.22", joints.compare_text(row["against_recommended"]))

    def test_a_hole_drawn_under_the_makers_does_not_fit(self):
        rep = self.trim_part("oval 0.9 2.5")
        self.assertEqual(self.one(rep)["verdict"], joints.DOES_NOT_FIT)
        self.assertFalse(rep["pass"])
        self.assertIn("drawn smaller than the maker's", joints.problems(rep)[0])

    def test_a_known_side_wider_than_the_hole_does_not_fit(self):
        self.assertEqual(self.one(self.trim_part("oval 1 2.5", {**self.TRIM_PEG, "width_mm": 2.6}))["verdict"],
                         joints.DOES_NOT_FIT)

    def test_a_whole_pin_that_fits_is_judged_by_the_pin_even_with_a_makers_hole(self):
        hole = {"shape": "round", "x_mm": 1.0, "y_mm": 1.0, "where": "p. 1"}
        row = self.one(self.run_on(footprint("J6", "HDR", pad("1", 1.1)),
                                   {"HDR": {"pins": [SQUARE_PIN], "recommended_hole": hole}}))
        self.assertEqual(row["verdict"], joints.FITS)

    def test_a_part_the_board_does_not_fit_is_not_judged_and_names_its_open_issue(self):
        issue = {"id": "debug_header_fit", "issue": "The debug header (J6) is not fitted."}
        for attr in ("(attr through_hole dnp)", "(attr through_hole exclude_from_bom)"):
            with self.subTest(attr=attr):
                rep = self.run_on(footprint("J6", "HDR", pad("1", 1.1), extra=f"\t\t{attr}\n".encode().decode()), {}, issues=[issue])
                row = self.one(rep)
                self.assertEqual(row["verdict"], joints.NOT_FITTED)
                self.assertEqual(row["open_issues"], ["debug_header_fit"])
                self.assertEqual((rep["counts"]["not_proven"], rep["counts"]["not_fitted"]), (0, 1))
                self.assertEqual(joints.unproven(rep), [])
                self.assertIn("not fitted (open issue debug_header_fit)", joints.markdown(rep))

    def test_pin_length_is_set_against_the_board_thickness(self):
        self.assertEqual(joints.length_text(self.TRIM_PEG, 1.6),
                         "0.90 against a 1.60 board: ends 0.70 inside the hole, short of the back face")
        self.assertEqual(joints.length_text({"length_mm": 3.0, "length_tol_mm": 0.3}, 1.6),
                         "3.00 +-0.30 against a 1.60 board: stands 1.40 past the back face")
        self.assertEqual(joints.length_text(SQUARE_PIN, 1.6), "not given")

    def test_markdown_names_parts_and_has_no_em_dash(self):
        rep = self.run_on(footprint("J5", "HDR", pad("1", 1.1)), {"HDR": {"pins": [SQUARE_PIN]}})
        text = joints.markdown(rep)
        self.assertIn("the sensor jack (J5)", text)
        self.assertNotIn("—", text)

    def test_exit_status_follows_the_failures(self):
        with tempfile.TemporaryDirectory() as folder:
            board = pathlib.Path(folder) / "b.kicad_pcb"
            board.write_text("(kicad_pcb\n" + footprint("J2", "HDR", pad("1", 0.75)) + ")\n", encoding="utf-8")
            sheet = pathlib.Path(folder) / "parts.json"
            sheet.write_text(json.dumps({"parts": {"HDR": {"pins": [SQUARE_PIN]}}}), encoding="utf-8")
            out = pathlib.Path(folder) / "out.json"
            argv = ["board_manual_joints.py", "--board", str(board), "--parts", str(sheet), "--json", str(out)]
            with mock.patch.object(sys, "argv", argv), mock.patch.object(sys, "stdout", io.TextIOWrapper(io.BytesIO())):
                self.assertEqual(joints.main(), 1)
            self.assertFalse(json.loads(out.read_text(encoding="utf-8"))["pass"])


if __name__ == "__main__":
    unittest.main()
