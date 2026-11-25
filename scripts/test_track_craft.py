#!/usr/bin/env python3
"""Exercise track-craft layer handling without starting KiCad."""

import importlib.util
import json
import math
import pathlib
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


F_CU, B_CU = 0, 2


def point(x, y):
    return types.SimpleNamespace(x=x, y=y)


class Box:
    def __init__(self, origin, size):
        self.left, self.top = origin.x, origin.y
        self.right, self.bottom = origin.x + size.x, origin.y + size.y

    def GetLeft(self): return self.left
    def GetTop(self): return self.top
    def GetRight(self): return self.right
    def GetBottom(self): return self.bottom
    def Intersects(self, other):
        return (self.left <= other.right and other.left <= self.right and
                self.top <= other.bottom and other.top <= self.bottom)

    def contains(self, pos):
        return self.left <= pos.x <= self.right and self.top <= pos.y <= self.bottom


class Pad:
    def __init__(self, ref, x, layer, net, size=(0.6, 0.2), shape=1, angle=0, footprint="R0603"):
        self.ref, self.position, self.layer, self.net, self.size = ref, point(x, 0), layer, net, size
        self.shape, self.angle, self.footprint = shape, angle, footprint

    def GetBoundingBox(self):
        return Box(point(self.position.x - self.size[0] / 2, -self.size[1] / 2), point(*self.size))

    def GetPosition(self): return self.position
    def GetNetname(self): return self.net
    def GetNumber(self): return "1"
    def GetShape(self, *args): return self.shape
    def ShapePos(self, layer): return self.position
    def GetSize(self, *args): return point(*self.size)
    def GetRoundRectRadiusRatio(self, layer): return 0.25
    def GetOrientationDegrees(self): return self.angle
    def GetAttribute(self): return 0
    def IsOnLayer(self, layer): return layer == self.layer
    def HitTest(self, pos, accuracy=0, layer=None):
        depth = checker.pad_boundary_depth(self, self.layer, pos.x, pos.y)
        return depth is not None and depth >= -accuracy
    def GetEffectiveShape(self, layer):
        return types.SimpleNamespace(Distance=lambda pos: max(0, -checker.pad_boundary_depth(self, layer, pos.x, pos.y)))


class Footprint:
    def __init__(self, pad): self.pad = pad
    def GetReference(self): return self.pad.ref
    def GetFPIDAsString(self): return self.pad.footprint
    def Pads(self): return [self.pad]


class Track:
    def __init__(self, net, layer, start, end, width):
        self.net, self.layer, self.start, self.end, self.width = net, layer, point(*start), point(*end), width

    def GetClass(self): return "PCB_TRACK"
    def GetNetname(self): return self.net
    def GetLayer(self): return self.layer
    def GetStart(self): return self.start
    def GetEnd(self): return self.end
    def GetWidth(self): return self.width
    def GetBoundingBox(self):
        half = self.width / 2
        return Box(point(min(self.start.x, self.end.x) - half, min(self.start.y, self.end.y) - half),
                   point(abs(self.start.x - self.end.x) + self.width, abs(self.start.y - self.end.y) + self.width))

    def HitTest(self, pos, tolerance):
        dx, dy = self.end.x - self.start.x, self.end.y - self.start.y
        length2 = dx * dx + dy * dy
        t = max(0, min(1, ((pos.x - self.start.x) * dx + (pos.y - self.start.y) * dy) / length2)) if length2 else 0
        return math.hypot(pos.x - (self.start.x + t * dx), pos.y - (self.start.y + t * dy)) <= self.width / 2 + tolerance


class Via:
    def __init__(self, net, x, y, layer, width):
        self.net, self.position, self.layer, self.width = net, point(x, y), layer, width

    def GetClass(self): return "PCB_VIA"
    def GetNetname(self): return self.net
    def GetPosition(self): return self.position
    def GetWidth(self, layer): return self.width
    def IsOnLayer(self, layer): return layer == self.layer
    def GetBoundingBox(self):
        return Box(point(self.position.x - self.width / 2, self.position.y - self.width / 2),
                   point(self.width, self.width))
    def HitTest(self, pos, tolerance):
        return math.hypot(pos.x - self.position.x, pos.y - self.position.y) <= self.width / 2 + tolerance


class Board:
    def __init__(self, renamed=False):
        self.names = {F_CU: "Top Layer", B_CU: "Bottom Layer"} if renamed else {F_CU: "F.Cu", B_CU: "B.Cu"}
        self.pads = [Pad("U1", 0, F_CU, "front", footprint="C0402"), Pad("U2", 5, B_CU, "back", footprint="C0402"),
                     Pad("U3", 10, B_CU, "wide"), Pad("U4", 15, B_CU, "wrong-front"),
                     Pad("U5", 20, F_CU, "wrong-back"), Pad("U6", 25, B_CU, "corner", (1, 1)),
                     Pad("U7", 30, F_CU, "other-copper")]
        self.tracks = [
            Track("front", F_CU, (0, 0), (1, 0), 0.15),
            Track("front", F_CU, (0.4, 0), (0.8, 0), 0.3),
            Track("back", B_CU, (5, 0), (6, 0), 0.15),
            Track("back", B_CU, (5.4, 0), (5.8, 0), 0.3),
            Track("wide", B_CU, (10, 0), (11, 0), 0.3),
            Track("wrong-front", F_CU, (15, 0), (16, 0), 0.3),
            Track("wrong-back", B_CU, (20, 0), (21, 0), 0.3),
            Track("corner", B_CU, (25, 0), (26, 0), 0.15),
            Track("corner", B_CU, (25, 0), (25, 1), 0.15),
            Track("other-copper", F_CU, (30, 0), (31, 0), 0.15),
            Track("other-copper", B_CU, (30.4, 0), (30.8, 0), 0.3),
            Track("angle", F_CU, (35, 0), (36, 0.2), 0.15),
        ]

    def GetFootprints(self): return [Footprint(pad) for pad in self.pads]
    def Tracks(self): return self.tracks
    def GetDrawings(self): return []
    def Zones(self): return []
    def GetLayerName(self, layer): return self.names[layer]
    @staticmethod
    def GetStandardLayerName(layer): return {F_CU: "F.Cu", B_CU: "B.Cu"}[layer]


fake_pcbnew = types.SimpleNamespace(
    ToMM=lambda value: value, FromMM=lambda value: value, VECTOR2I=point, BOX2I=Box,
    F_Cu=F_CU, B_Cu=B_CU, PAD_SHAPE_CIRCLE=0, PAD_SHAPE_RECTANGLE=1,
    PAD_SHAPE_OVAL=2, PAD_SHAPE_ROUNDRECT=4, PAD_ATTRIB_PTH=1, SHAPE_T_POLY=1,
)
with patch.dict(sys.modules, {"pcbnew": fake_pcbnew}):
    path = pathlib.Path(__file__).resolve().parents[1] / "tools" / "kicad" / "check_track_craft.py"
    spec = importlib.util.spec_from_file_location("_track_craft_test", path)
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)


class TrackCraftLayerTests(unittest.TestCase):
    def test_renamed_layers_produce_identical_readable_findings(self):
        standard = checker.analyse(Board())
        renamed = checker.analyse(Board(renamed=True))
        self.assertEqual(standard, renamed)
        self.assertIn("F.Cu", standard["segment not 0/45/90"][0])
        self.assertTrue(any("B.Cu" in row and "U6.1" in row
                            for row in standard["corner covered by pad or via (allowed)"]))

    def test_pad_and_copper_layers_are_respected_on_both_sides(self):
        findings = checker.analyse(Board(renamed=True))
        overhangs = findings["track end cap outside the pad outline"]
        self.assertTrue(any("U3.1" in row for row in overhangs))
        self.assertFalse(any("U4.1" in row or "U5.1" in row for row in overhangs))
        envelope = findings["neck copper wider than the neck within 0.3 mm of the pad"]
        self.assertTrue(any("U1.1 F.Cu" in row for row in envelope))
        self.assertTrue(any("U2.1 B.Cu" in row for row in envelope))
        self.assertFalse(any("U7.1" in row for row in envelope))


class TrackCraftGeometryTests(unittest.TestCase):
    def test_0402_package_token_does_not_match_metric_01005_name(self):
        self.assertTrue(checker.footprint_is_inch_0402("library:C0402__hash__C10"))
        self.assertTrue(checker.footprint_is_inch_0402("Resistor_SMD:R_0402_1005Metric"))
        self.assertFalse(checker.footprint_is_inch_0402("Capacitor_SMD:C_01005_0402Metric"))

    def test_roundrect_depth_uses_actual_perimeter(self):
        pad = Pad("U3", 0, F_CU, "N", size=(0.6, 0.24), shape=4)
        self.assertAlmostEqual(checker.pad_boundary_depth(pad, F_CU, 0, 0), 0.12)
        self.assertAlmostEqual(0.30 / 2 - checker.pad_boundary_depth(pad, F_CU, 0, 0), 0.03)
        self.assertLess(checker.pad_boundary_depth(pad, F_CU, 0.30, 0.12), 0)
        self.assertAlmostEqual(checker.pad_exit_distance(pad, F_CU, 0, 0, 1, 0), 0.3)
        self.assertAlmostEqual(checker.pad_exit_distance(pad, F_CU, 0, 0, 0, 1), 0.12)

    def test_rotated_rect_and_circle_depth(self):
        pad = Pad("X", 0, F_CU, "N", size=(1, 0.4), angle=90)
        self.assertAlmostEqual(checker.pad_boundary_depth(pad, F_CU, 0.15, 0), 0.05)
        circle = Pad("C", 0, F_CU, "N", size=(0.5, 0.5), shape=0)
        self.assertAlmostEqual(checker.pad_boundary_depth(circle, F_CU, 0.15, 0), 0.10)
        self.assertAlmostEqual(checker.pad_boundary_depth(circle, F_CU, 0.15, 0.15),
                               0.25 - math.hypot(0.15, 0.15))

    def test_via_gap_follows_pad_copper_not_its_bounding_box(self):
        circle = Pad("C", 0, F_CU, "N", size=(1, 1), shape=0)
        via = types.SimpleNamespace(IsOnLayer=lambda layer: layer == F_CU,
                                    GetPosition=lambda: point(0.4, 0.4),
                                    GetWidth=lambda layer: 0.1)
        self.assertAlmostEqual(checker.pad_via_gap(circle, via, F_CU), math.hypot(0.4, 0.4) - 0.5 - 0.05)
        self.assertIsNone(checker.pad_via_gap(circle, via, B_CU))

    def test_internal_diagonal_is_not_a_pad_entry(self):
        bd = Board()
        bd.pads = [Pad("P", 0, F_CU, "N", size=(2, 2))]
        bd.tracks = [Track("N", F_CU, (0, 0), (0.25, 0.25), 0.2),
                     Track("N", F_CU, (0.25, 0.25), (0.25, 2), 0.2)]
        self.assertEqual(checker.analyse(bd).get("track meets pad off its axis", []), [])
        bd.tracks = [Track("N", F_CU, (0, 0), (2, 2), 0.2)]
        self.assertEqual(len(checker.analyse(bd)["track meets pad off its axis"]), 1)

    def test_narrow_pad_escape_does_not_require_exact_015(self):
        bd = Board()
        bd.pads = [Pad("U3", 0, F_CU, "N", size=(0.6, 0.24), shape=4)]
        bd.tracks = [Track("N", F_CU, (0, 0), (1, 0), 0.22)]
        findings = checker.analyse(bd)
        self.assertFalse(findings.get("track end cap outside the pad outline"))
        self.assertFalse(findings.get("unnecked track into a 0.3 mm pin"))

    def test_45_degree_junction_is_a_manual_outline_check_not_a_failure(self):
        bd = Board()
        bd.pads = []
        bd.tracks = [Track("N", F_CU, (0, 0), (1, 0), 0.2),
                     Track("N", F_CU, (0, 0), (0, 1), 0.2),
                     Track("N", F_CU, (0, 0), (1, 1), 0.2)]
        findings = checker.analyse(bd)
        self.assertTrue(findings["acute centreline: inspect copper outline (inspect)"])
        self.assertFalse(findings.get("acute angle"))

    def test_bend_cap_fully_inside_pad_needs_no_extra_margin(self):
        bd = Board()
        bd.pads = [Pad("P", 0, F_CU, "N", size=(1, 1))]
        bd.tracks = [Track("N", F_CU, (0, 0), (0.39, 0), 0.2),
                     Track("N", F_CU, (0.39, 0), (0.39, 1), 0.2)]
        findings = checker.analyse(bd)
        self.assertTrue(findings["bend inside a pad, fully covered by its copper (allowed)"])
        self.assertFalse(findings.get("bend inside the pad outline"))
        bd.tracks = [Track("N", F_CU, (0, 0), (0.42, 0), 0.2),
                     Track("N", F_CU, (0.42, 0), (0.42, 1), 0.2)]
        self.assertTrue(checker.analyse(bd)["bend inside the pad outline"])

    def test_diode_bend_is_hidden_by_pad_and_straight_incoming_track(self):
        bd = Board()
        bd.pads = [Pad("D3", 0, F_CU, "N", size=(1, 0.65))]
        bd.tracks = [Track("N", F_CU, (0.2465, -2), (0.2465, -0.2465), 0.25),
                     Track("N", F_CU, (0.2465, -0.2465), (0, 0), 0.25)]
        findings = checker.analyse(bd)
        self.assertFalse(findings.get("track end cap outside the pad outline"))
        self.assertFalse(findings.get("bend inside the pad outline"))
        self.assertTrue(findings.get("end cap: no exposed samples (inspect)"))
        self.assertTrue(findings.get("bend: no exposed samples (inspect)"))

    def test_corner_near_pad_edge_uses_union_not_pad_alone(self):
        bd = Board()
        bd.pads = [Pad("P", 0, F_CU, "N", size=(1, 1))]
        bd.tracks = [Track("N", F_CU, (0.45, 0), (1, 0), 0.2),
                     Track("N", F_CU, (0.45, 0), (0.45, 0.25), 0.2)]
        findings = checker.analyse(bd)
        self.assertFalse(findings.get("90 degree corner on a single track"))
        self.assertTrue(findings.get("corner: no exposed samples (inspect)"))

    def test_0402_via_ring_counts_beyond_a_compliant_020_gap(self):
        bd = Board()
        bd.pads = [Pad("C10", 0, F_CU, "N", size=(0.6, 0.5), footprint="C0402")]
        bd.tracks = [Track("N", F_CU, (0, 0), (1.4, 0), 0.25),
                     Via("N", 0.8, 0, F_CU, 0.6)]
        findings = checker.analyse(bd)
        self.assertFalse(findings.get("via within 0.2 mm of a same-net pad"))
        self.assertTrue(findings.get("neck copper wider than the neck within 0.3 mm of the pad"))

    def test_wide_0402_segment_wholly_inside_pad_is_not_an_escape(self):
        bd = Board()
        bd.pads = [Pad("C10", 0, F_CU, "N", size=(1, 0.8), footprint="C0402")]
        bd.tracks = [Track("N", F_CU, (-0.1, 0), (0.1, 0), 0.3)]
        findings = checker.analyse(bd)
        self.assertFalse(findings.get("track wider than 0.25 into an 0402 pad"))
        self.assertFalse(findings.get("wide internal 0402 segment (inspect)"))

    def test_0402_via_on_other_layer_does_not_count_in_envelope(self):
        bd = Board()
        bd.pads = [Pad("C10", 0, F_CU, "N", size=(0.6, 0.5), footprint="C0402")]
        bd.tracks = [Track("N", F_CU, (0, 0), (1.4, 0), 0.25),
                     Via("N", 0.8, 0, B_CU, 0.6)]
        findings = checker.analyse(bd)
        self.assertFalse(findings.get("neck copper wider than the neck within 0.3 mm of the pad"))

    def test_0402_neck_and_offset_via_are_measured_as_a_union(self):
        bd = Board()
        bd.pads = [Pad("C10", 0, F_CU, "N", size=(0.6, 0.4), footprint="C0402")]
        bd.tracks = [Track("N", F_CU, (0, 0), (1.4, 0), 0.25),
                     Via("N", 0.88, 0.2, F_CU, 0.6)]
        findings = checker.analyse(bd)
        self.assertFalse(findings.get("via within 0.2 mm of a same-net pad"))
        self.assertTrue(findings.get("neck copper wider than the neck within 0.3 mm of the pad"))


class TrackCraftRunnerSafetyTests(unittest.TestCase):
    def test_missing_board_fails_fast_without_calling_load_board(self):
        with tempfile.TemporaryDirectory() as td:
            missing = pathlib.Path(td) / "missing.kicad_pcb"
            with patch.object(checker.pcbnew, "LoadBoard", create=True, side_effect=AssertionError("must not call LoadBoard")):
                with self.assertRaises(FileNotFoundError):
                    checker.load(str(missing))
                self.assertEqual(checker.main(str(missing)), 2)

    def test_output_creates_parent_directory_and_writes_report(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            pcb = root / "demo.kicad_pcb"
            pcb.write_text("(kicad_pcb (version 20241229))\n", encoding="utf-8")
            out = root / "nested" / "reports" / "track-craft.json"
            clean_board = Board()
            clean_board.pads = []
            clean_board.tracks = []
            with patch.object(checker.pcbnew, "LoadBoard", create=True, return_value=clean_board):
                rc = checker.main(str(pcb), output=str(out))
            self.assertEqual(rc, 0)
            data = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(data["reported_failing_items"], 0)
            self.assertIn("board_sha256", data)

    def test_board_track_craft_local_and_remote_dispatch(self):
        scripts_dir = pathlib.Path(__file__).resolve().parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        import board_track_craft  # noqa: E402
        import mac  # noqa: E402

        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            pcb = root / "board.kicad_pcb"
            pcb.write_text("(kicad_pcb)\n", encoding="utf-8")
            out = root / "out" / "tc.json"
            missing = root / "missing.kicad_pcb"

            self.assertEqual(board_track_craft.main([str(missing)]), 2)

            with (
                patch.object(board_track_craft, "ROOT", root),
                patch.object(mac, "LOCAL", True),
                patch.object(board_track_craft, "_local_kicad_python", return_value="kicad-py"),
                patch("subprocess.run", return_value=subprocess.CompletedProcess(["x"], 0, "ok\n", "")) as mocked,
            ):
                rc = board_track_craft.main([str(pcb), "--output", str(out), "--timeout", "45"])
            self.assertEqual(rc, 0)
            self.assertTrue(out.parent.is_dir())
            self.assertEqual(mocked.call_args.kwargs["timeout"], 45)

            payload = json.dumps({"reported_failing_items": 1, "inspection_candidates": 0, "findings": {"x": ["y"]}})
            stdout_bytes = (
                f"craft check: 1 failing items, 0 inspection candidates\n"
                f"{board_track_craft.REPORT_START}\n{payload}\n{board_track_craft.REPORT_END}\n"
            ).encode("utf-8")
            remote_out = root / "remote" / "tc.json"
            with (
                patch.object(board_track_craft, "ROOT", root),
                patch.object(mac, "LOCAL", False),
                patch.object(mac, "HOST", "bot@mac"),
                patch.object(mac, "KEY", root / "id_key"),
                patch("subprocess.run", return_value=subprocess.CompletedProcess(["ssh"], 1, stdout_bytes, b"")) as ssh_mock,
            ):
                rc2 = board_track_craft.main([str(pcb), "--output", str(remote_out)])
            self.assertEqual(rc2, 1)
            self.assertEqual(ssh_mock.call_args.args[0][0], "ssh")
            self.assertEqual(json.loads(remote_out.read_text(encoding="utf-8"))["reported_failing_items"], 1)


if __name__ == "__main__":
    unittest.main()
