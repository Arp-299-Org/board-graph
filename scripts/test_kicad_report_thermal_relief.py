#!/usr/bin/env python3
"""The thermal relief report on boards this test builds itself, and its line in the board checks.

Run with normal Python: the board checks' handling of a report runs everywhere. Set BOARD_GRAPH_TEST_KICAD=1 on the
Mac to also build three small boards with KiCad's Python (on Windows it opens a
window, so that half runs on the Mac only) and run the report on each:
  solid      a ground plane on In1.Cu and In2.Cu, filled by pcbnew; pad 1 set solid, pad 2 thermal
  setting    the same fill, then pad 1's setting cleared to thermal and saved without a refill
  refilled   the setting board refilled by pcbnew
Each also has a surface-mount ground pad and a through-hole pad on a net without a zone, which are counted only.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import site_settings

SCRIPTS = pathlib.Path(__file__).resolve().parent
ROOT = SCRIPTS.parent


def build(directory: str) -> None:
    """Runs in KiCad's Python: write solid.kicad_pcb, setting.kicad_pcb and refilled.kicad_pcb into directory."""
    import pcbnew

    def at(x: float, y: float):
        return pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y))

    board = pcbnew.BOARD()
    board.SetCopperLayerCount(4)
    gnd, sig = pcbnew.NETINFO_ITEM(board, "GND"), pcbnew.NETINFO_ITEM(board, "SIG")
    board.Add(gnd)
    board.Add(sig)
    edge = pcbnew.PCB_SHAPE(board, pcbnew.SHAPE_T_RECTANGLE)
    edge.SetLayer(pcbnew.Edge_Cuts)
    edge.SetStart(at(0, 0))
    edge.SetEnd(at(30, 20))
    board.Add(edge)

    fp = pcbnew.FOOTPRINT(board)
    fp.SetReference("J1")
    fp.SetPosition(at(0, 0))
    board.Add(fp)

    def through_hole(number: str, x: float, net, connection: int):
        pad = pcbnew.PAD(fp)
        pad.SetAttribute(pcbnew.PAD_ATTRIB_PTH)
        pad.SetLayerSet(pcbnew.PAD.PTHMask())
        pad.SetShape(pcbnew.F_Cu, pcbnew.PAD_SHAPE_CIRCLE)
        pad.SetSize(pcbnew.F_Cu, pcbnew.VECTOR2I(pcbnew.FromMM(2.0), pcbnew.FromMM(2.0)))
        pad.SetDrillSize(pcbnew.VECTOR2I(pcbnew.FromMM(1.0), pcbnew.FromMM(1.0)))
        pad.SetNumber(number)
        pad.SetPosition(at(x, 10))
        pad.SetNet(net)
        pad.SetLocalZoneConnection(connection)
        fp.Add(pad)
        return pad

    solid = through_hole("1", 8, gnd, pcbnew.ZONE_CONNECTION_FULL)
    through_hole("2", 15, gnd, pcbnew.ZONE_CONNECTION_INHERITED)
    through_hole("3", 22, sig, pcbnew.ZONE_CONNECTION_INHERITED)
    smd = pcbnew.PAD(fp)
    smd.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
    smd.SetLayerSet(pcbnew.PAD.SMDMask())
    smd.SetShape(pcbnew.F_Cu, pcbnew.PAD_SHAPE_RECTANGLE)
    smd.SetSize(pcbnew.F_Cu, pcbnew.VECTOR2I(pcbnew.FromMM(1.0), pcbnew.FromMM(1.0)))
    smd.SetNumber("4")
    smd.SetPosition(at(15, 16))
    smd.SetNet(gnd)
    fp.Add(smd)

    layers = pcbnew.LSET()
    layers.AddLayer(pcbnew.In1_Cu)
    layers.AddLayer(pcbnew.In2_Cu)
    zone = pcbnew.ZONE(board)
    zone.SetLayerSet(layers)
    zone.SetNet(gnd)
    zone.SetPadConnection(pcbnew.ZONE_CONNECTION_THERMAL)
    zone.SetThermalReliefGap(pcbnew.FromMM(0.5))
    zone.SetThermalReliefSpokeWidth(pcbnew.FromMM(0.4))
    zone.SetLocalClearance(pcbnew.FromMM(0.3))
    zone.SetMinThickness(pcbnew.FromMM(0.2))
    outline = zone.Outline()
    outline.NewOutline()
    for x, y in ((1, 1), (29, 1), (29, 19), (1, 19)):
        outline.Append(pcbnew.FromMM(x), pcbnew.FromMM(y))
    board.Add(zone)

    out = pathlib.Path(directory)
    filler = pcbnew.ZONE_FILLER(board)
    filler.Fill(board.Zones())
    pcbnew.SaveBoard(str(out / "solid.kicad_pcb"), board)
    solid.SetLocalZoneConnection(pcbnew.ZONE_CONNECTION_INHERITED)
    pcbnew.SaveBoard(str(out / "setting.kicad_pcb"), board)
    filler.Fill(board.Zones())
    pcbnew.SaveBoard(str(out / "refilled.kicad_pcb"), board)


class BoardCheckLine(unittest.TestCase):
    """validate_board's check reads the report the same way for a pass, a finding and a tool that did not run."""

    def run_check(self, report: dict | None, returncode: int = 0) -> dict:
        sys.path.insert(0, str(SCRIPTS))
        import validate_board

        with tempfile.TemporaryDirectory(prefix="bg-thermal-") as name:
            out = pathlib.Path(name)

            def offline(script, *args):
                self.assertEqual(script, "scripts/kicad_report_thermal_relief.py")
                if report is not None:
                    pathlib.Path(args[args.index("--json") + 1]).write_text(json.dumps(report), encoding="utf-8")
                return subprocess.CompletedProcess([], returncode, "", "pcbnew failed")

            with mock.patch.object(validate_board, "kicad_offline", offline):
                return validate_board.check_thermal_relief(out)

    def test_pass(self):
        got = self.run_check({"through_hole_pads_checked": 10, "findings": []})
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["report"], "thermal-relief.json")

    def test_finding_fails_and_names_the_pad(self):
        finding = {"ref": "R14", "pad": "7", "x_mm": 122.0, "y_mm": 78.3,
                   "layers": [{"layer": "In1.Cu (GND_REF_TOP)"}, {"layer": "In2.Cu (GND_REF_BOTTOM)"}]}
        got = self.run_check({"through_hole_pads_checked": 10, "findings": [finding]})
        self.assertFalse(got["ok"], got)
        self.assertIn("R14.7 (122.0, 78.3)", got["detail"])
        self.assertIn("In2.Cu", got["detail"])

    def test_no_report_is_not_a_pass(self):
        self.assertFalse(self.run_check(None, returncode=1)["ok"])
        self.assertFalse(self.run_check({"through_hole_pads_checked": 0, "findings": []}, returncode=1)["ok"])


@unittest.skipUnless(site_settings.env("BOARD_GRAPH_TEST_KICAD") == "1" and sys.platform == "darwin",
                     "set BOARD_GRAPH_TEST_KICAD=1 on the Mac to build the boards in KiCad")
class ReportOnBuiltBoards(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / "tools" / "kicad"))
        from kicad_install import KICAD_PYTHON

        cls.python = str(KICAD_PYTHON)
        cls.directory = tempfile.TemporaryDirectory(prefix="bg-thermal-")
        made = subprocess.run([cls.python, __file__, "--build", cls.directory.name],
                              capture_output=True, text=True, check=False)
        if made.returncode != 0:
            raise RuntimeError(made.stdout + made.stderr)
        cls.reports = {}
        for case in ("solid", "setting", "refilled"):
            report = pathlib.Path(cls.directory.name) / f"{case}.json"
            done = subprocess.run([cls.python, str(SCRIPTS / "kicad_report_thermal_relief.py"), "--board",
                                   str(pathlib.Path(cls.directory.name) / f"{case}.kicad_pcb"), "--json", str(report)],
                                  capture_output=True, text=True, check=False)
            if done.returncode != 0:
                raise RuntimeError(done.stdout + done.stderr)
            cls.reports[case] = json.loads(report.read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def shares(self, case: str) -> dict:
        return {(r["pad"], r["layer"]): (r["connection"], r["band_share"], r["finding"])
                for r in self.reports[case]["rows"]}

    def test_counts(self):
        for report in self.reports.values():
            self.assertEqual(report["through_hole_pads_checked"], 2, report)
            self.assertEqual(report["through_hole_pads_net_without_zone"], 1, report)
            self.assertEqual(report["surface_mount_pads"], 1, report)
            self.assertEqual(len(report["rows"]), 4, report)  # two ground pads on two planes

    def test_solid_setting_is_a_finding(self):
        report = self.reports["solid"]
        self.assertEqual([(f["ref"], f["pad"]) for f in report["findings"]], [("J1", "1")])
        self.assertEqual(sorted(a["layer"] for a in report["findings"][0]["layers"]), ["In1.Cu", "In2.Cu"])
        shares = self.shares("solid")
        for layer in ("In1.Cu", "In2.Cu"):
            self.assertEqual(shares[("1", layer)][0], "solid")
            self.assertGreater(shares[("1", layer)][1], report["threshold"])
            self.assertEqual(shares[("2", layer)][0], "thermal")
            self.assertLess(shares[("2", layer)][1], report["threshold"])
            self.assertFalse(shares[("2", layer)][2])

    def test_thermal_setting_on_a_solid_fill_is_a_finding(self):
        report = self.reports["setting"]
        self.assertEqual([(f["ref"], f["pad"]) for f in report["findings"]], [("J1", "1")])
        for at in report["findings"][0]["layers"]:
            self.assertEqual(at["connection"], "thermal")
            self.assertGreater(at["band_share"], report["threshold"])

    def test_refilled_board_has_no_finding(self):
        report = self.reports["refilled"]
        self.assertEqual(report["findings"], [], report["rows"])
        self.assertTrue(all(r["fill_touches_pad"] for r in report["rows"]), report["rows"])


if __name__ == "__main__":
    if sys.argv[1:2] == ["--build"]:
        build(sys.argv[2])
    else:
        unittest.main(verbosity=2)
