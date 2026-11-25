"""Release warning allowances follow reviewed evidence, without an obsolete part list; the packager reads the
board checks' reports by the names the checks write."""
from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys
import tempfile
import types
import unittest
import zipfile

# Fixture board description and machine settings, read at import so every checkout derives the same names.
FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
os.environ["BOARD_GRAPH_BOARD"] = str(FIXTURES / "board.json")
os.environ["BOARD_GRAPH_SITE"] = str(FIXTURES / "board_graph_site.json")
import kicad_finalize_jlc_outputs as finalize  # noqa: E402
import kicad_generate_fabrication_archive as fabrication  # noqa: E402
import site_settings  # noqa: E402
import kicad_package_production as packager  # noqa: E402
from kicad_package_production import documented_drc_warnings, pad_mask_complete  # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools" / "kicad"))
from kicad_install import KICAD_CLI  # noqa: E402

KICAD_INSTALLED = KICAD_CLI.is_file()


class PadMaskTests(unittest.TestCase):
    def test_every_pad_is_accounted_for_including_the_net_ties(self):
        self.assertTrue(pad_mask_complete({"findings": [], "pads_checked": 262, "pads_skipped": {"net_tie": 2}}, 264))
        self.assertFalse(pad_mask_complete({"findings": [], "pads_checked": 262}, 264))
        self.assertFalse(pad_mask_complete({"findings": [{"ref": "J1"}], "pads_checked": 262, "pads_skipped": {"net_tie": 2}}, 264))
        self.assertFalse(pad_mask_complete({"findings": []}, 264))


class SavedBoardCountTests(unittest.TestCase):
    def test_a_board_without_its_own_audit_is_counted_from_its_text(self):
        text = """(kicad_pcb (version 20250000)
  (footprint "lib:R" (at 1 2) (pad "1" smd rect (at 0 0)) (pad "2" smd rect (at 1 0)))
  (footprint "lib:J" (at 5 2) (pad "1" thru_hole circle (at 0 0)) (pad "" np_thru_hole circle (at 2 0))
    (fp_line (start 0 0) (end 1 1)))
  (segment (start 0 0) (end 1 0) (width 0.2) (layer "F.Cu") (net 1))
  (segment (start 1 0) (end 2 0) (width 0.2) (layer "F.Cu") (net 1))
  (arc (start 2 0) (mid 2.5 0.5) (end 3 0) (width 0.2) (layer "F.Cu") (net 1))
  (via (at 3 0) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1)))
"""
        self.assertEqual(packager.saved_board_counts(text), {"tracks": 2, "vias": 1, "pads": 4})


class ReportNameTests(unittest.TestCase):
    def test_the_packager_reads_what_the_board_checks_write(self):
        source = (pathlib.Path(__file__).with_name("validate_board.py")).read_text(encoding="utf-8")
        written = set(re.findall(r'"([a-z-]+\.json)"', source))  # every report name the checks write
        self.assertTrue(set(packager.CHECK_REPORTS.values()) <= written,
                        set(packager.CHECK_REPORTS.values()) - written)
        # The packager's default G2 list is the one the board checks read: the board description's drc_exclusions.
        self.assertEqual(packager.EXCLUSIONS_DEFAULT.name, "documented-drc-exclusions-final.json")
        self.assertEqual(packager.EXCLUSIONS_DEFAULT, packager.board_description.load().path("drc_exclusions"))
        self.assertIn('DOCUMENTED_DRC = description.path("drc_exclusions")', source)



def board_text(inner):
    """A saved board with a front, `inner` inner and a back copper layer, in the order KiCad 10 writes them (the
    back layer after the inner ones, numbered 2), one inner layer renamed, and a track."""
    rows = ['(0 "F.Cu" signal)'] + [f'({4 + 2 * n} "In{n + 1}.Cu" power{" " + chr(34) + "GND_REF" + chr(34) if n == 0 else ""})'
                                    for n in range(inner)]
    rows += ['(2 "B.Cu" signal)', '(13 "F.Paste" user)', '(15 "B.Paste" user)', '(5 "F.SilkS" user "F.Silkscreen")',
             '(7 "B.SilkS" user "B.Silkscreen")', '(1 "F.Mask" user)', '(3 "B.Mask" user)',
             '(25 "Edge.Cuts" user "Multi-Layer")']
    return ('(kicad_pcb (version 20241229) (generator "pcbnew") (generator_version "9.0")\n (general (thickness 1.6))\n'
            ' (paper "A4")\n (layers\n  ' + "\n  ".join(rows) + ')\n (setup (pad_to_mask_clearance 0))\n (net 0 "")\n'
            ' (gr_rect (start 0 0) (end 20 10) (stroke (width 0.1) (type default)) (fill no) (layer "Edge.Cuts"))\n'
            ' (footprint "lib:R" (layer "F.Cu") (at 5 5) (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask")))\n'
            ' (segment (start 2 2) (end 10 2) (width 0.25) (layer "F.Cu") (net 0))\n)\n')


class CopperLayerTests(unittest.TestCase):
    """The fabrication archive's copper layers are the board's own, two, four or more, and the generator, the
    assembly check and the packager all take them from the one place."""

    def copper(self, inner):
        with tempfile.TemporaryDirectory() as folder:
            board = pathlib.Path(folder) / "b.kicad_pcb"
            board.write_text(board_text(inner), encoding="utf-8")
            return fabrication.board_copper(board)

    def test_the_copper_layers_are_read_from_the_saved_board_in_stack_order(self):
        self.assertEqual(self.copper(0), ["F.Cu", "B.Cu"])
        self.assertEqual(self.copper(2), ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])
        self.assertEqual(self.copper(4), ["F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu"])
        # A pad's own (layers ...) is not the board's list, and ten inner layers sort by number, not by text.
        self.assertEqual(self.copper(10)[1:-1], [f"In{n}.Cu" for n in range(1, 11)])

    def test_a_board_without_a_front_and_a_back_copper_layer_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            board = pathlib.Path(folder) / "b.kicad_pcb"
            board.write_text('(kicad_pcb (version 1) (layers (0 "F.Cu" signal) (25 "Edge.Cuts" user)))', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, r"lists copper layers \['F.Cu'\]"):
                fabrication.board_copper(board)
            board.write_text("(kicad_pcb (version 1))", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "lists copper layers none"):
                fabrication.board_copper(board)

    def test_the_layers_and_extensions_follow_the_copper(self):
        other = ["F.Silkscreen", "B.Silkscreen", "F.Mask", "B.Mask", "F.Paste", "B.Paste", "Edge.Cuts"]
        self.assertEqual(fabrication.fabrication_layers(["F.Cu", "B.Cu"]), ["F.Cu", "B.Cu", *other])
        self.assertEqual(fabrication.fabrication_layers(["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]),
                         ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu", *other])  # the four-layer list the scripts held before
        rest = {".gto", ".gbo", ".gts", ".gbs", ".gtp", ".gbp", ".gm1", ".gbrjob"}
        self.assertEqual(fabrication.fabrication_extensions(["F.Cu", "B.Cu"]), {".gtl", ".gbl"} | rest)
        self.assertEqual(fabrication.fabrication_extensions(["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]),
                         {".gtl", ".gbl", ".g1", ".g2"} | rest)
        self.assertEqual(fabrication.fabrication_extensions(self.copper(4)),
                         {".gtl", ".gbl", ".g1", ".g2", ".g3", ".g4"} | rest)

    def test_the_three_scripts_take_the_layers_from_one_place(self):
        scripts = pathlib.Path(__file__).resolve().parent
        self.assertIs(packager.fabrication_layers, fabrication.fabrication_layers)
        self.assertIs(packager.board_copper, fabrication.board_copper)
        self.assertIs(packager.board_extra_layers, fabrication.board_extra_layers)
        self.assertIs(finalize.board_copper, fabrication.board_copper)
        self.assertIs(finalize.board_extra_layers, fabrication.board_extra_layers)
        self.assertIs(finalize.fabrication_extensions, fabrication.fabrication_extensions)
        self.assertIs(finalize.fabrication_member_suffixes, fabrication.fabrication_member_suffixes)
        for name in ("kicad_generate_fabrication_archive.py", "kicad_package_production.py",
                     "kicad_finalize_jlc_outputs.py"):
            self.assertNotIn("In1.Cu", (scripts / name).read_text(encoding="utf-8"), name)

    def test_extra_user_comments_layer_detection_and_suffixes(self):
        base = board_text(0)
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            plain = root / "plain.kicad_pcb"
            plain.write_text(base.replace(
                '(25 "Edge.Cuts" user "Multi-Layer")',
                '(25 "Edge.Cuts" user "Multi-Layer")\n  (40 "Cmts.User" user)',
            ), encoding="utf-8")
            self.assertEqual(fabrication.board_extra_layers(plain), [])

            hidden_prop = root / "hidden.kicad_pcb"
            hidden_prop.write_text(base.replace(
                '(pad "1" smd rect',
                '(property "Note" "x" (layer "Cmts.User") (effects (hide yes))) (pad "1" smd rect',
            ), encoding="utf-8")
            self.assertEqual(fabrication.board_extra_layers(hidden_prop), [])

            top_cmts = root / "top_cmts.kicad_pcb"
            top_cmts.write_text(base.replace(
                '(segment ',
                '(gr_rect (start 1 1) (end 8 8) (stroke (width 0.1) (type default)) (fill no) (layer "Cmts.User"))\n (segment ',
            ), encoding="utf-8")
            self.assertEqual(fabrication.board_extra_layers(top_cmts), ["Cmts.User"])

            fp_cmts = root / "fp_cmts.kicad_pcb"
            fp_cmts.write_text(base.replace(
                '(pad "1" smd rect',
                '(fp_line (start 0 0) (end 2 2) (layer "User.Comments")) (pad "1" smd rect',
            ), encoding="utf-8")
            self.assertEqual(fabrication.board_extra_layers(fp_cmts), ["Cmts.User"])

        self.assertEqual(
            fabrication.fabrication_layers(["F.Cu", "B.Cu"], ["Cmts.User"]),
            ["F.Cu", "B.Cu", *fabrication.OTHER_LAYERS, "Cmts.User"],
        )
        self.assertEqual(fabrication.fabrication_member_suffixes(["Cmts.User"]), {"-user_comments.gbr"})
        self.assertEqual(fabrication.fabrication_member_suffixes(()), set())

    @unittest.skipUnless(site_settings.env("BOARD_GRAPH_TEST_KICAD") == "1" and fabrication.KICAD_CLI.is_file()
                         and KICAD_INSTALLED, "set BOARD_GRAPH_TEST_KICAD=1 where KiCad is installed to export "
                                              "the archives with kicad-cli (no window opens)")
    def test_kicad_writes_an_archive_with_the_boards_own_copper_layers(self):
        for inner in (0, 2, 4):
            with self.subTest(copper=inner + 2), tempfile.TemporaryDirectory() as folder:
                folder = pathlib.Path(folder)
                board = folder / "b.kicad_pcb"
                board.write_text(board_text(inner), encoding="utf-8")
                done = subprocess.run([sys.executable, str(fabrication.__file__), "--board", str(board), "--production",
                                       str(folder / "production"), "--base-name", "X"], capture_output=True, text=True)
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                with zipfile.ZipFile(folder / "production" / "X.zip") as archive:
                    found = {pathlib.PurePosixPath(name).suffix.lower() for name in archive.namelist()}
                expected = fabrication.fabrication_extensions(fabrication.board_copper(board))
                self.assertEqual(expected - found, set())
                self.assertEqual({item for item in found if re.fullmatch(r"\.g\d+", item)},
                                 {f".g{n}" for n in range(1, inner + 1)})

    @unittest.skipUnless(site_settings.env("BOARD_GRAPH_TEST_KICAD") == "1" and fabrication.KICAD_CLI.is_file()
                         and KICAD_INSTALLED, "set BOARD_GRAPH_TEST_KICAD=1 where KiCad is installed")
    def test_kicad_flex_zero_part_board_exports_and_verifies_user_comments(self):
        flex_pcb = (
            '(kicad_pcb (version 20241229) (generator "pcbnew") (generator_version "9.0")\n'
            ' (general (thickness 0.3))\n'
            ' (paper "A4")\n'
            ' (layers\n'
            '  (0 "F.Cu" signal)\n'
            '  (2 "B.Cu" signal)\n'
            '  (13 "F.Paste" user)\n'
            '  (15 "B.Paste" user)\n'
            '  (5 "F.SilkS" user "F.Silkscreen")\n'
            '  (7 "B.SilkS" user "B.Silkscreen")\n'
            '  (1 "F.Mask" user)\n'
            '  (3 "B.Mask" user)\n'
            '  (40 "Cmts.User" user)\n'
            '  (25 "Edge.Cuts" user))\n'
            ' (setup (pad_to_mask_clearance 0))\n'
            ' (net 0 "")\n'
            ' (gr_rect (start 0 0) (end 40 12) (stroke (width 0.1) (type default)) (fill no) (layer "Edge.Cuts"))\n'
            ' (gr_rect (start 2 2) (end 10 10) (stroke (width 0.15) (type default)) (fill no) (layer "Cmts.User"))\n'
            ' (gr_text "0.30 +/- 0.05 mm / 35 um flex stiffener" (at 20 6 0) (layer "Cmts.User")\n'
            '  (effects (font (size 1 1) (thickness 0.15))))\n'
            ')\n'
        )
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            board = root / "octave_strip_v1.kicad_pcb"
            board.write_text(flex_pcb, encoding="utf-8")
            (root / "octave_strip_v1.kicad_pro").write_text("{}", encoding="utf-8")
            (root / "octave_strip_v1.kicad_dru").write_text("(version 1)\n", encoding="utf-8")
            prod = root / "production"
            done = subprocess.run(
                [sys.executable, str(fabrication.__file__), "--board", str(board),
                 "--production", str(prod), "--base-name", "Strip"],
                capture_output=True, text=True,
            )
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            zip_path = prod / "Strip.zip"
            with zipfile.ZipFile(zip_path) as archive:
                names = {pathlib.PurePosixPath(name).name for name in archive.namelist()}
            self.assertIn("octave_strip_v1-User_Comments.gbr", names)
            verified = packager.verify_fabrication_derives_from_board(zip_path, board)
            self.assertTrue(verified["all_layers_match_fresh_export"])
            self.assertIn("Cmts.User", verified["layers_exported"])


def warning(ref, kind="lib_footprint_mismatch", severity="warning"):
    return {"type": kind, "severity": severity,
            "items": [{"description": f"Footprint {ref}"}]}


class WarningAllowanceTests(unittest.TestCase):
    def test_current_references_and_explicit_empty_allowance(self):
        self.assertEqual(documented_drc_warnings(
            {"violations": [warning("C11")]}, {"accepted_warning_refs": ["C11"]}),
            [{"reference": "C11", "excluded": False}])
        self.assertEqual(documented_drc_warnings(
            {"violations": []}, {"accepted_warning_refs": []}), [])

    def test_missing_duplicate_or_invalid_allowance_is_not_empty_permission(self):
        for allowance in ({}, {"accepted_warning_refs": ["C11", "C11"]},
                          {"accepted_warning_refs": ["*"]}):
            with self.subTest(allowance=allowance), self.assertRaises(RuntimeError):
                documented_drc_warnings({"violations": []}, allowance)

    def test_added_removed_duplicate_or_electrical_findings_require_review(self):
        for violations in ([], [warning("C12")], [warning("C11"), warning("C11")],
                           [warning("C11", "clearance")], [warning("C11", severity="error")]):
            with self.subTest(violations=violations), self.assertRaises(RuntimeError):
                documented_drc_warnings({"violations": violations}, {"accepted_warning_refs": ["C11"]})


class FinalizeJlcOutputsTests(unittest.TestCase):
    def test_group_smt_bom_rows_allows_functional_labels_and_rejects_specs_or_colors(self):
        grouped, labels = finalize.group_smt_bom_rows([
            {"Designator": "D1", "Footprint": "LED_0603__abc__D1", "Quantity": "1", "Value": "PWR", "LCSC Part #": "C2286"},
            {"Designator": "D2", "Footprint": "LED_0603__def__D2", "Quantity": "1", "Value": "RUN", "LCSC Part #": "C2286"},
            {"Designator": "R1", "Footprint": "R_0603__1__R1", "Quantity": "1", "Value": "10k", "LCSC Part #": "C25804"},
            {"Designator": "R2", "Footprint": "R_0603__2__R2", "Quantity": "1", "Value": "10k", "LCSC Part #": "C25804"},
        ])
        self.assertEqual(labels, {"C2286": ["PWR", "RUN"]})
        self.assertEqual(grouped[0], {
            "Designator": "D1, D2",
            "Footprint": "LED_0603",
            "Quantity": "2",
            "Value": "PWR / RUN",
            "LCSC Part #": "C2286",
        })
        self.assertEqual(grouped[1]["Value"], "10k")
        self.assertEqual(grouped[1]["Designator"], "R1, R2")

        for val_a, val_b in (("10k", "100k"), ("100nF", "1uF"), ("PWR", "10k"), ("Green", "Red"), ("PWR_GREEN", "ERR_RED")):
            with self.subTest(values=(val_a, val_b)), self.assertRaisesRegex(RuntimeError, "used with two values"):
                finalize.group_smt_bom_rows([
                    {"Designator": "X1", "Footprint": "FP", "Quantity": "1", "Value": val_a, "LCSC Part #": "C999"},
                    {"Designator": "X2", "Footprint": "FP", "Quantity": "1", "Value": val_b, "LCSC Part #": "C999"},
                ])

    def test_empty_zero_part_bom_and_cpl_preserve_headers(self):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            bom_in = root / "Strip_bom.csv"
            cpl_in = root / "Strip_positions.csv"
            finalize.write_csv(bom_in, [], finalize.BOM_FIELDS)
            finalize.write_csv(cpl_in, [], finalize.CPL_FIELDS)
            bom_rows, bom_fields = finalize.read_csv_with_fields(bom_in, finalize.BOM_FIELDS)
            cpl_rows, cpl_fields = finalize.read_csv_with_fields(cpl_in, finalize.CPL_FIELDS)
            self.assertEqual((bom_rows, bom_fields), ([], finalize.BOM_FIELDS))
            self.assertEqual((cpl_rows, cpl_fields), ([], finalize.CPL_FIELDS))
            empty = root / "empty.csv"
            empty.write_text("", encoding="utf-8")
            self.assertEqual(finalize.read_csv_with_fields(empty, finalize.BOM_FIELDS), ([], finalize.BOM_FIELDS))

    def test_validate_fabrication_archive_checks_optional_cmts_user_with_zero_parts(self):
        stub_pcbnew = types.SimpleNamespace(
            F_Paste=1,
            B_Paste=2,
            PAD_ATTRIB_SMD=1,
            PAD_ATTRIB_NPTH=3,
        )
        stub_board = types.SimpleNamespace(GetFootprints=lambda: [])
        old_pcbnew = finalize.pcbnew
        finalize.pcbnew = stub_pcbnew
        try:
            with tempfile.TemporaryDirectory() as folder:
                root = pathlib.Path(folder)
                zip_path = root / "Strip.zip"
                base_members = [
                    "strip-F_Cu.gtl", "strip-B_Cu.gbl",
                    "strip-F_Silkscreen.gto", "strip-B_Silkscreen.gbo",
                    "strip-F_Mask.gts", "strip-B_Mask.gbs",
                    "strip-F_Paste.gtp", "strip-B_Paste.gbp",
                    "strip-Edge_Cuts.gm1", "strip-job.gbrjob",
                    "strip-PTH.drl", "strip-NPTH.drl",
                ]
                with zipfile.ZipFile(zip_path, "w") as archive:
                    for name in base_members:
                        archive.writestr(name, "")
                errs_plain, flashes = finalize.validate_fabrication_archive(zip_path, stub_board, ["F.Cu", "B.Cu"], [])
                self.assertEqual(errs_plain, [])
                self.assertEqual(flashes, {".gtp": 0, ".gbp": 0})

                errs_missing, _ = finalize.validate_fabrication_archive(zip_path, stub_board, ["F.Cu", "B.Cu"], ["Cmts.User"])
                self.assertTrue(any("-user_comments.gbr" in msg for msg in errs_missing), errs_missing)

                with zipfile.ZipFile(zip_path, "a") as archive:
                    archive.writestr("strip-User_Comments.gbr", "")
                errs_ok, _ = finalize.validate_fabrication_archive(zip_path, stub_board, ["F.Cu", "B.Cu"], ["Cmts.User"])
                self.assertEqual(errs_ok, [])
        finally:
            finalize.pcbnew = old_pcbnew


if __name__ == "__main__":
    unittest.main()
