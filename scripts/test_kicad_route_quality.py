#!/usr/bin/env python3
"""Pad coverage regressions for both route-audit inputs, without KiCad."""

import pathlib
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import kicad_route_quality as quality

F_CU, B_CU = 3, 34
NM = 1_000_000


def vector(x, y):
    return types.SimpleNamespace(x=round(x * NM), y=round(y * NM))


class Pad:
    def __init__(self, net="N", position=(0, 0), layers=(F_CU,), shape=2, size=(2, 2),
                 angle=0, ratio=0.25, offset=(0, 0), number="1"):
        self.name, self.position_mm, self.layers, self.number = net, position, set(layers), number
        self.shape, self.size, self.angle, self.ratio, self.offset = shape, size, angle, ratio, offset
        self.net = types.SimpleNamespace(name=net)
        self.position = vector(*position)
        geometry = types.SimpleNamespace(layer=F_CU, shape=shape, size=vector(*size),
                                         offset=vector(*offset), corner_rounding_ratio=ratio)
        self.padstack = types.SimpleNamespace(type=1, copper_layers=[geometry],
                                              angle=types.SimpleNamespace(degrees=angle))

    def GetNetname(self): return self.name
    def GetNumber(self): return self.number
    def GetPosition(self): return self.position
    def IsOnLayer(self, layer): return layer in self.layers
    def FlashLayer(self, layer): return layer in self.layers
    def GetShape(self, layer): return self.shape
    def GetSize(self, layer): return vector(*self.size)
    def ShapePos(self, layer):
        # These integration fixtures use zero offset; the rotation and offset
        # adapter is tested separately against a known point.
        return self.position
    def GetOrientationDegrees(self): return self.angle
    def GetRoundRectRadiusRatio(self, layer): return self.ratio


class Track:
    def __init__(self, start, end, layer, width, net):
        self.start, self.end = vector(*start), vector(*end)
        self.layer, self.width = layer, round(width * NM)
        self.net = types.SimpleNamespace(name=net)

    def GetNetname(self): return self.net.name
    def GetLayer(self): return self.layer
    def GetStart(self): return self.start
    def GetEnd(self): return self.end
    def GetWidth(self): return self.width


class Arc(Track):
    """A track arc as both bindings present it: a Track with a midpoint (kipy), a PCB_ARC subclass (pcbnew)."""
    def __init__(self, start, mid, end, layer, width, net):
        super().__init__(start, end, layer, width, net)
        self.mid = vector(*mid)

    def GetMid(self): return self.mid


class Via:
    def __init__(self, position=(0, 0), diameter=0.6, net="N"):
        self.position, self.diameter = vector(*position), round(diameter * NM)
        self.net = types.SimpleNamespace(name=net)

    def GetNetname(self): return self.net.name
    def GetPosition(self): return self.position
    def GetWidth(self, layer): return self.diameter


class Board:
    def __init__(self, pads, tracks, vias=()):
        self.pads, self.tracks, self.vias = pads, tracks, list(vias)
        self.document = types.SimpleNamespace(project=types.SimpleNamespace(path="/board"))

    def get_footprints(self):
        reference = types.SimpleNamespace(text=types.SimpleNamespace(value="U1"))
        return [types.SimpleNamespace(definition=types.SimpleNamespace(pads=self.pads), reference_field=reference)]
    def get_tracks(self): return self.tracks
    def get_vias(self): return self.vias
    def check_padstack_presence_on_layers(self, pads, layers):
        return {pad: {layer: layer in pad.layers for layer in layers} for pad in pads}
    def GetFootprints(self): return [types.SimpleNamespace(Pads=lambda: self.pads, GetReference=lambda: "U1")]
    def GetTracks(self): return self.tracks + self.vias


def modules(board):
    kipy = types.ModuleType("kipy")
    kipy.KiCad = lambda **kw: types.SimpleNamespace(get_board=lambda: board)
    proto = types.ModuleType("kipy.proto.board.board_types_pb2")
    proto.BoardLayer = types.SimpleNamespace(BL_F_Cu=F_CU, BL_B_Cu=B_CU, BL_In1_Cu=4)
    proto.PadStackShape = types.SimpleNamespace(PSS_CIRCLE=1, PSS_RECTANGLE=2, PSS_OVAL=3, PSS_ROUNDRECT=5)
    proto.PadStackType = types.SimpleNamespace(PST_NORMAL=1, PST_FRONT_INNER_BACK=2)
    pcbnew = types.ModuleType("pcbnew")
    pcbnew.LoadBoard = lambda path: board
    pcbnew.ToMM = lambda value: value / NM
    pcbnew.PCB_VIA, pcbnew.PCB_TRACK, pcbnew.PCB_ARC = Via, Track, Arc
    pcbnew.PAD_SHAPE_CIRCLE, pcbnew.PAD_SHAPE_RECTANGLE = 1, 2
    pcbnew.PAD_SHAPE_OVAL, pcbnew.PAD_SHAPE_ROUNDRECT = 3, 5
    return {"kipy": kipy, "kipy.proto": types.ModuleType("kipy.proto"),
            "kipy.proto.board": types.ModuleType("kipy.proto.board"),
            "kipy.proto.board.board_types_pb2": proto, "pcbnew": pcbnew}


def audit_pair(pad, corner=(0.3, 0.2), layer=F_CU, widths=(0.2, 0.2), net="N"):
    x, y = corner
    tracks = [Track((x - 1, y), corner, layer, widths[0], net),
              Track(corner, (x, y + 1), layer, widths[1], net)]
    board = Board([pad], tracks)
    with patch.dict(sys.modules, modules(board)):
        live, _ = quality.audit_live_board(pathlib.Path("/board"))
        offline, _ = quality.audit_offline_board(pathlib.Path("/board/demo.kicad_pcb"))
    return [item.rule for item in live], [item.rule for item in offline]


class AuditCoverage(unittest.TestCase):
    def test_segment_intersection_uses_the_pad_outline(self):
        rect = quality.PadCopper((0, 0), (2, 1), 0, "rect")
        self.assertTrue(rect.intersects_segment((-2, -2), (2, 2)))
        self.assertFalse(rect.intersects_segment((-2, 0.6), (2, 0.6)))
        circle = quality.PadCopper((0, 0), (2, 2), 0, "circle")
        self.assertTrue(circle.intersects_segment((-2, 0.9), (2, 0.9)))
        self.assertFalse(circle.intersects_segment((0.9, 0.9), (2, 2)))
        rotated = quality.PadCopper((3, 4), (2, 0.4), 90, "rect")
        self.assertTrue(rotated.intersects_segment((2, 4), (4, 4)))
        self.assertFalse(rotated.intersects_segment((3.3, 2), (3.3, 6)))

    def test_collinear_width_transition_must_not_be_merged(self):
        for end_width, expected in ((0.3, []), (0.2, ["mergeable_collinear_segments"])):
            board = Board([], [Track((0, 0), (0.5, 0), F_CU, 0.2, "N"),
                               Track((0.5, 0), (2, 0), F_CU, end_width, "N")])
            with patch.dict(sys.modules, modules(board)):
                live, _ = quality.audit_live_board(pathlib.Path("/board"), board)
                offline, _ = quality.audit_offline_board(pathlib.Path("/board/demo.kicad_pcb"))
            self.assertEqual([item.rule for item in live], expected)
            self.assertEqual([item.rule for item in offline], expected)

    def assert_corner(self, pad, *, expected, **kwargs):
        live, offline = audit_pair(pad, **kwargs)
        self.assertEqual(live, offline)
        self.assertEqual("hard_90_corner" in live, expected)

    def test_buried_offcenter_corner_and_layers(self):
        self.assert_corner(Pad(), expected=False)
        self.assert_corner(Pad(), expected=True, layer=B_CU)
        self.assert_corner(Pad(layers=(F_CU, B_CU)), expected=False, layer=B_CU)
        self.assert_corner(Pad(net="other"), expected=True)

    def test_endcap_and_larger_of_two_track_widths(self):
        self.assert_corner(Pad(), expected=True, corner=(0.95, 0), widths=(0.2, 0.2))
        self.assert_corner(Pad(), expected=True, corner=(0.86, 0), widths=(0.1, 0.3))
        self.assert_corner(Pad(), expected=False, corner=(0.8, 0), widths=(0.1, 0.3))

    def test_roundrect_and_rotated_pad_in_both_audits(self):
        self.assert_corner(Pad(shape=5, size=(2, 1)), expected=True, corner=(0.95, 0.45), widths=(0.1, 0.1))
        self.assert_corner(Pad(shape=5, size=(2, 1)), expected=False, corner=(0.6, 0.2), widths=(0.1, 0.1))
        self.assert_corner(Pad(size=(2, 0.4), angle=90), expected=False, corner=(0, 0.7), widths=(0.1, 0.1))
        self.assert_corner(Pad(size=(2, 0.4), angle=90), expected=True, corner=(0.7, 0), widths=(0.1, 0.1))

    def test_unknown_shape_does_not_exempt_corner(self):
        self.assert_corner(Pad(shape=7), expected=True)

    def test_round_and_oval_are_not_bounding_boxes(self):
        for shape in ("circle", "oval", "roundrect"):
            pad = quality.PadCopper((0, 0), (2, 2), 0, shape, 0.5)
            with self.subTest(shape=shape):
                self.assertTrue(pad.contains_endcap((0.3, 0.3), 0.2))
                self.assertFalse(pad.contains_endcap((0.9, 0.9), 0.05))

    def test_live_rotates_offset_into_board_coordinates(self):
        pad = Pad(size=(0.4, 1.0), angle=90, offset=(0.5, 0))
        board = Board([pad], [])
        with patch.dict(sys.modules, modules(board)):
            copper = quality._live_pad_copper(board, [pad], {F_CU})[("N", F_CU)][0]
        self.assertAlmostEqual(copper.center[0], 0)
        self.assertAlmostEqual(copper.center[1], -0.5)
        self.assertTrue(copper.contains_endcap((0, -0.5), 0.1))

    def test_corner_under_a_via_ring_is_the_ring(self):
        # Two tracks meeting at 90 degrees at a via centre: the inside corner lies under the annulus, so the copper
        # outline there is the ring's arc. A ring too small to cover the end cap, or a via off the corner, still
        # leaves a corner.
        corner = (123.17, 105.605)
        tracks = [Track((124.05, 105.605), corner, F_CU, 0.25, "N"), Track(corner, (123.17, 106.55), F_CU, 0.3, "N")]
        for via, expected in ((Via(corner, 0.6), []), (Via(corner, 0.25), ["hard_90_corner"]),
                              (Via((123.17, 105.4), 0.6), ["hard_90_corner"]), (Via(corner, 0.6, net="M"), ["hard_90_corner"])):
            board = Board([], tracks, [via])
            with patch.dict(sys.modules, modules(board)):
                live, summary = quality.audit_live_board(pathlib.Path("/board"), board)
                offline, _ = quality.audit_offline_board(pathlib.Path("/board/demo.kicad_pcb"))
            self.assertEqual([item.rule for item in live], expected, via.diameter)
            self.assertEqual([item.rule for item in offline], expected, via.diameter)
            self.assertEqual(summary["vias"], 1)
        # Two tracks meeting at 45 degrees at the same via: their inside corner lies 2.6 half-widths out, past a
        # 0.6 mm ring, so the sharp wedge the earlier review found still surfaces.
        acute = [Track((124.05, 105.605), corner, F_CU, 0.25, "N"), Track(corner, (123.67, 105.105), F_CU, 0.3, "N")]
        board = Board([], acute, [Via(corner, 0.6)])
        with patch.dict(sys.modules, modules(board)):
            live, _ = quality.audit_live_board(pathlib.Path("/board"), board)
            offline, _ = quality.audit_offline_board(pathlib.Path("/board/demo.kicad_pcb"))
        self.assertEqual([item.rule for item in live], ["acute_or_reversing_corner"])
        self.assertEqual([item.rule for item in offline], ["acute_or_reversing_corner"])

    def test_track_arcs_are_reported_not_ignored(self):
        # An arc is outside the 45-degree rules; both audits name it as an error and count it apart from the tracks.
        board = Board([], [Track((0, 0), (1, 0), F_CU, 0.3, "N"), Arc((1, 0), (1.7, 0.3), (2, 1), F_CU, 0.3, "N")])
        with patch.dict(sys.modules, modules(board)):
            live, live_summary = quality.audit_live_board(pathlib.Path("/board"), board)
            offline, offline_summary = quality.audit_offline_board(pathlib.Path("/board/demo.kicad_pcb"))
        for findings, summary in ((live, live_summary), (offline, offline_summary)):
            self.assertEqual([item.rule for item in findings], ["track_arc"])
            self.assertEqual((summary["tracks"], summary["arcs"], summary["errors"]), (1, 1, 1))
            self.assertEqual(findings[0].point_mm, (1.0, 0.0))

    def grazed(self, pads, tracks):
        """The pads both audits name as grazed, as (label, layer); the two must agree."""
        board = Board(pads, tracks)
        with patch.dict(sys.modules, modules(board)):
            live, _ = quality.audit_live_board(pathlib.Path("/board"), board)
            offline, _ = quality.audit_offline_board(pathlib.Path("/board/demo.kicad_pcb"))
        found = [[(item.message.split(":")[0], item.layer) for item in findings if item.rule == "track_grazes_pad"]
                 for findings in (live, offline)]
        self.assertEqual(found[0], found[1])
        return found[0]

    def test_encoder_tracks_laid_for_the_old_pads_graze_the_new_ones(self):
        # 91fd297e: the encoder (ENC1) A track ran up the east edge of pad 1, its centre line on the pad's edge,
        # after the part changed and its pads moved 0.75 mm. Main lays it into the pad's middle.
        pad = Pad("/ENC_A", (201.858, 94.549), shape=2, size=(1.5, 2.4), ratio=0)
        before = [Track((175.04, 96.6), (202.108, 96.6), F_CU, 0.25, "/ENC_A"),
                  Track((202.108, 96.6), (202.608, 96.1), F_CU, 0.25, "/ENC_A"),
                  Track((202.608, 96.1), (202.608, 93.249), F_CU, 0.25, "/ENC_A")]
        after = [Track((201.858, 96.00802), (201.26602, 96.6), F_CU, 0.25, "/ENC_A"),
                 Track((201.26602, 96.6), (175.04, 96.6), F_CU, 0.25, "/ENC_A"),
                 Track((201.858, 94.549), (201.858, 96.00802), F_CU, 0.25, "/ENC_A")]
        self.assertEqual(self.grazed([pad], before), [("U1.1", F_CU)])
        self.assertEqual(self.grazed([pad], after), [])

    def test_capacitor_pad_joined_only_by_a_clipped_corner(self):
        # 64cd0ed8: the 3V3_A diagonal clipped the corner of C1's pad 2 and stopped outside it. Main adds a track
        # into the pad's middle; the diagonal still clips the corner, and the pad is entered.
        pad = Pad("3V3_A", (116.345, 94.5), shape=2, size=(0.79, 0.54), ratio=0, number="2")
        diagonal = Track((115.7, 93), (117.2, 94.5), F_CU, 0.4, "3V3_A")
        self.assertEqual(self.grazed([pad], [diagonal, Track((117.2, 94.5), (117.934, 94.5), F_CU, 0.4, "3V3_A")]),
                         [("U1.2", F_CU)])
        self.assertEqual(self.grazed([pad], [diagonal, Track((116.345, 94.5), (117.934, 94.5), F_CU, 0.4, "3V3_A")]),
                         [])

    def test_a_pad_narrower_than_its_track_is_entered_at_its_middle(self):
        # An IC's 0.24 mm pin (U3.13) takes a 0.25 mm track ending at its centre: the pad has only
        # 0.12 mm of depth to give. Stopping short of the middle is still a graze.
        pad = Pad("AMP_EN", (123.85, 104.3), size=(0.24, 0.6), ratio=0)
        self.assertEqual(self.grazed([pad], [Track((123.85, 104.3), (122.843, 104.3), F_CU, 0.25, "AMP_EN")]), [])
        self.assertEqual(self.grazed([pad], [Track((123.75, 104.3), (122.843, 104.3), F_CU, 0.25, "AMP_EN")]),
                         [("U1.1", F_CU)])

    def test_a_track_crossing_the_pad_joins_it(self):
        # R44's pad sits on a 0.4 mm 3V3_D track 0.234 mm off its centre: the centre line crosses the pad and leaves.
        # The same track stopping there is a graze, a polyline turning inside the pad and leaving crosses it, and a
        # centre line on the pad's edge does not cross it.
        pad = Pad("3V3_D", (185.0, 95.327), size=(0.864, 0.8065), ratio=0)
        through = Track((174.209, 95.561), (193.003, 95.561), F_CU, 0.4, "3V3_D")
        self.assertEqual(self.grazed([pad], [through]), [])
        self.assertEqual(self.grazed([pad], [Track((174.209, 95.561), (185.0, 95.561), F_CU, 0.4, "3V3_D")]),
                         [("U1.1", F_CU)])
        self.assertEqual(self.grazed([pad], [Track((174.209, 95.561), (185.0, 95.561), F_CU, 0.4, "3V3_D"),
                                             Track((185.0, 95.561), (185.5, 96.061), F_CU, 0.4, "3V3_D")]), [])
        self.assertEqual(self.grazed([pad], [Track((174.209, 95.73), (193.003, 95.73), F_CU, 0.4, "3V3_D")]),
                         [("U1.1", F_CU)])

    def test_pad_entry_uses_the_real_pad_shape(self):
        # A circle's bounding box would hold this end; the circle does not. A pad turned 90 degrees is 0.4 mm wide
        # across x, so a track up x = 0.2 runs along its edge, not 0.8 mm inside it.
        circle = Pad("N", (0, 0), shape=1, size=(1, 1))
        self.assertEqual(self.grazed([circle], [Track((1, 1), (0.4, 0.4), F_CU, 0.2, "N")]), [("U1.1", F_CU)])
        self.assertEqual(self.grazed([circle], [Track((1, 1), (0.2, 0.2), F_CU, 0.2, "N")]), [])
        turned = Pad("N", (0, 0), size=(2, 0.4), angle=90, ratio=0)
        self.assertEqual(self.grazed([turned], [Track((0.2, 2), (0.2, -2), F_CU, 0.2, "N")]), [("U1.1", F_CU)])
        self.assertEqual(self.grazed([turned], [Track((0, 2), (0, 0.5), F_CU, 0.2, "N")]), [])
        oval = Pad("N", (0, 0), shape=3, size=(1, 2))
        self.assertEqual(self.grazed([oval], [Track((1, 0.9), (0.3, 0.9), F_CU, 0.2, "N")]), [("U1.1", F_CU)])

    def test_each_copper_layer_of_a_through_hole_pad_is_judged(self):
        pad = Pad("N", (0, 0), layers=(F_CU, B_CU), shape=1, size=(1.7, 1.7))
        into = Track((3, 0), (0, 0), F_CU, 0.25, "N")
        along = Track((0.85, 3), (0.85, -3), B_CU, 0.25, "N")
        self.assertEqual(self.grazed([pad], [into]), [])
        self.assertEqual(self.grazed([pad], [into, along]), [("U1.1", B_CU)])

    def test_other_nets_and_untouched_pads_are_not_judged(self):
        pad = Pad("N", (0, 0), size=(1, 1), ratio=0)
        self.assertEqual(self.grazed([pad], [Track((0.5, 3), (0.5, -3), F_CU, 0.25, "M")]), [])
        self.assertEqual(self.grazed([pad], [Track((0.7, 3), (0.7, -3), F_CU, 0.25, "N")]), [])
        self.assertEqual(self.grazed([Pad("N", (0, 0), shape=7)], [Track((1, 3), (1, -3), F_CU, 0.25, "N")]), [])

    def test_padless_polyline_retains_original_rules(self):
        self.assertTrue(any("90-degree" in error for error in quality.validate_polyline([(0, 0), (1, 0), (1, 1)])))
        self.assertFalse(quality.validate_polyline([(0, 0), (1, 0), (2, 1)]))


class MicroSegmentTests(unittest.TestCase):
    """Micro-segment checks accept fine-pitch fanouts, buried same-net copper, and T-junction splits, while still
    catching real unburied short jogs and duplicate stubs."""

    def audit(self, tracks, pads=(), vias=()):
        board = Board(list(pads), list(tracks), list(vias))
        with patch.dict(sys.modules, modules(board)):
            live, _ = quality.audit_live_board(pathlib.Path("/board"))
            offline, _ = quality.audit_offline_board(pathlib.Path("/board.kicad_pcb"))
        self.assertEqual(live, offline)
        return [item for item in live if item.rule == "micro_segment"]

    def test_fine_pitch_45_degree_fanout_above_0_15_mm_passes(self):
        # 0.226 mm diagonal fanout typical of a 0.50 mm pitch socket: clean, not a micro-segment.
        tracks = [
            Track((0.0, 0.0), (1.0, 0.0), F_CU, 0.30, "/MOTOR_A"),
            Track((1.0, 0.0), (1.16, 0.16), F_CU, 0.30, "/MOTOR_A"),
            Track((1.16, 0.16), (2.5, 0.16), F_CU, 0.30, "/MOTOR_A"),
        ]
        self.assertEqual(self.audit(tracks), [])

    def test_unburied_micro_segment_below_0_15_mm_still_warns(self):
        # 0.10 mm free-standing 45-degree jog between two same-width legs still warns.
        tracks = [
            Track((0.0, 0.0), (1.0, 0.0), F_CU, 0.30, "/MOTOR_A"),
            Track((1.0, 0.0), (1.07, 0.07), F_CU, 0.30, "/MOTOR_A"),
            Track((1.07, 0.07), (2.0, 0.07), F_CU, 0.30, "/MOTOR_A"),
        ]
        found = self.audit(tracks)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].severity, "warning")

    def test_width_step_jog_buried_in_wider_track_endcap_passes(self):
        # 0.0145 mm 0.30 mm segment at a 0.50 -> 0.30 mm width transition lies entirely inside the 0.50 mm cap.
        tracks = [
            Track((0.0, 0.0), (2.0, 0.0), F_CU, 0.50, "/MOTOR_B"),
            Track((2.0, 0.0), (2.0145, 0.0), F_CU, 0.30, "/MOTOR_B"),
            Track((2.0145, 0.0), (2.5145, 0.5), F_CU, 0.30, "/MOTOR_B"),
        ]
        self.assertEqual(self.audit(tracks), [])

    def test_t_junction_collinear_split_of_straight_track_passes_but_degree_2_split_warns(self):
        # A 0.50 mm straight track split 0.0228 mm before a bend by a T-junction branch is one straight run.
        with_branch = [
            Track((0.0, 0.0), (1.9772, 0.0), F_CU, 0.50, "/MOTOR_B"),
            Track((1.9772, 0.0), (2.0, 0.0), F_CU, 0.50, "/MOTOR_B"),
            Track((1.9772, 0.0), (1.9772, -1.5), F_CU, 0.30, "/MOTOR_B"),
            Track((2.0, 0.0), (2.5, 0.5), F_CU, 0.50, "/MOTOR_B"),
        ]
        self.assertEqual(self.audit(with_branch), [])

        # Without the branch (degree-2 collinear split), the tiny segment still warns.
        without_branch = [with_branch[0], with_branch[1], with_branch[3]]
        self.assertEqual(len(self.audit(without_branch)), 1)

        # A tiny T-junction bridge between two bends (total straight run < 0.15 mm) still warns.
        tiny_bridge = [
            Track((0.0, -1.0), (1.0, 0.0), F_CU, 0.30, "/MOTOR_B"),
            Track((1.0, 0.0), (1.03, 0.0), F_CU, 0.30, "/MOTOR_B"),
            Track((1.0, 0.0), (1.0, 1.0), F_CU, 0.30, "/MOTOR_B"),
            Track((1.03, 0.0), (2.03, 1.0), F_CU, 0.30, "/MOTOR_B"),
        ]
        self.assertEqual(len(self.audit(tiny_bridge)), 1)

    def test_segment_buried_in_pad_or_via_passes_and_duplicate_micro_segments_do_not_hide_each_other(self):
        pad = Pad("/N", (0.0, 0.0), size=(2.0, 2.0), ratio=0)
        in_pad = [Track((0.1, 0.0), (0.15, 0.0), F_CU, 0.25, "/N")]
        self.assertEqual(self.audit(in_pad, pads=[pad]), [])

        via = Via((5.0, 5.0), diameter=0.8, net="/N")
        in_via = [Track((5.02, 5.0), (5.07, 5.0), F_CU, 0.25, "/N")]
        self.assertEqual(self.audit(in_via, vias=[via]), [])

        duplicates = [
            Track((0.0, 0.0), (1.0, 0.0), F_CU, 0.25, "/N"),
            Track((1.0, 0.0), (1.05, 0.05), F_CU, 0.25, "/N"),
            Track((1.0, 0.0), (1.05, 0.05), F_CU, 0.25, "/N"),
            Track((1.05, 0.05), (2.0, 0.05), F_CU, 0.25, "/N"),
        ]
        self.assertEqual(len(self.audit(duplicates)), 2)


if __name__ == "__main__":
    unittest.main()
