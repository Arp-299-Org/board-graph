#!/usr/bin/env python3
"""Tests of scripts/kicad_route.py with no KiCad: the coordinate and refusal logic, against a stand-in editor.

  py -3 scripts/test_kicad_route.py
"""
from __future__ import annotations

import dataclasses
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import kicad_route as kr  # noqa: E402
from kicad_route import Item, Mapping, PadInfo, Panel, Request, RouteFailure, Run, Status  # noqa: E402

SAFE = (400.0, 130.0, 1600.0, 800.0)


def pad(name, net, x, y, layers=("F.Cu",), half=0.4, through=False):
    return PadInfo(name, net, x, y, layers, half, through)


class FakeBoard:
    """IPC's view of the board, which the fake editor's router changes."""

    def __init__(self, pads, items=()):
        self.pads = {p.name: p for p in pads}
        self.tracks = list(items)
        self.layer = "F.Cu"
        self.saved = False

    def pad(self, spec):
        if spec not in self.pads:
            raise RouteFailure("bad_request", f"no pad {spec}")
        return self.pads[spec]

    def items(self):
        return list(self.tracks)

    def pad_boxes(self):
        return [(p.name, p.net, (p.x - p.half, p.y - p.half, p.x + p.half, p.y + p.half)) for p in self.pads.values()]

    def active_layer(self):
        return self.layer

    def set_active_layer(self, name):
        self.layer = name

    def wait_ready(self):
        pass

    def clear_selection(self):
        pass

    def save(self):
        self.saved = True

    def text(self):
        return ""

    def quality(self):
        return {}


class FakeEditor:
    """A canvas: screen = scale * board + offset. Its status bar snaps to the grid, like KiCad's; its router starts
    only on a pad's copper, and refuses `dirty` pads."""

    def __init__(self, board, scale=20.0, offset=(300.0, -1000.0), grid=0.25, dirty=(), via_ends_route=True):
        self.board, self.scale, self.offset, self.grid = board, scale, offset, grid
        self.dirty, self.via_ends_route = set(dirty), via_ends_route
        self.pointer = (900.0, 500.0)
        self.keys, self.clicks, self.menus = [], [], []
        self.routing = None  # (net, last point)
        self.armed_via = False
        self.router_mode = "Walk around"
        self.mode_sticks = True
        self.modes_set = []
        self.track_width = 0.2
        self.width_sticks = True
        self.widths_set = []
        self.via_lands = None  # where the router sets a via when it gets stuck short of the pointer
        self.strip = ["Zoom 5.95"]
        self.commits = []
        self.view_cache = None
        self.garble_status = False
        self.shift_after_clicks = None
        self.undone = 0
        self.next_id = 0

    # What the tool calls.
    def activate(self): pass
    def wait(self, _seconds): pass
    def safe_rect(self): return SAFE
    def picture(self, _box, name): return f"/tmp/{name}.png"
    def title_clean(self): return True
    def forget_view(self): self.view_cache = None
    def save_view(self, _status, mapping): self.view_cache = mapping
    def load_view(self, _status): return self.view_cache

    def raw(self):
        return ((self.pointer[0] - self.offset[0]) / self.scale, (self.pointer[1] - self.offset[1]) / self.scale)

    def read_status(self):
        if self.garble_status:
            raise RouteFailure("mapping_failed", "the status bar did not read as a position in mm")
        x, y = self.raw()
        over = self._pad_at((x, y)) if self.routing else None
        if over:  # an armed router snaps the cursor to the pad under the pointer
            return Status(over.x, over.y, self.scale / 4, self.grid)
        snap = lambda v: round(v / self.grid) * self.grid  # noqa: E731
        return Status(snap(x), snap(y), self.scale / 4, self.grid)

    def read_at(self, sx, sy):
        self.pointer = (sx, sy)
        return self.read_status()

    def read_panel(self):
        if self.routing:
            return Panel(True, self.routing[0], self.router_mode, self.track_width)
        return Panel(False, None, None, None)

    def set_router_mode(self, name):
        self.modes_set.append(name)
        if self.mode_sticks:
            self.router_mode = name

    def set_track_width(self, width_mm):
        self.widths_set.append(width_mm)
        if self.width_sticks:
            self.track_width = width_mm

    def read_strip(self):
        return list(self.strip)

    def key(self, name, command=False):
        self.keys.append(("cmd+" if command else "") + name)
        if name == "x":
            self._start_router()
        elif name == "f4":  # center on the pointer, and the pointer follows
            self.offset = (self.offset[0] + 1000 - self.pointer[0], self.offset[1] + 465 - self.pointer[1])
            self.pointer = (1000.0, 465.0)
        elif name == "v":
            self.armed_via = True
        elif name == "esc":
            self.routing, self.armed_via = None, False
        elif name == "z" and command and self.commits:
            self.undone += 1
            for item in self.commits.pop():
                self.board.tracks.remove(item)

    def click(self, sx, sy):
        self.clicks.append((sx, sy))
        self.pointer = (sx, sy)
        at = self.raw()
        if self.shift_after_clicks is not None:
            self.shift_after_clicks -= 1
            if self.shift_after_clicks == 0:
                self.offset = (self.offset[0] + 200, self.offset[1])
        if not self.routing:
            return
        net, last = self.routing
        target = self._pad_at(at)
        # The placed copper uses the same snapped cursor as the status bar.
        end = (target.x, target.y) if target else tuple(round(v / self.grid) * self.grid for v in at)
        joined = any(item.net == net and end in (item.start, item.end) for item in self.board.tracks)
        from kicad_route_quality import canonical_45_path
        path = canonical_45_path(last, end, balance=0 if target else 1 if self._pad_at(last) else 0.5)
        commit = [self._track(net, a, b, self.board.layer) for a, b in zip(path, path[1:])]
        if self.armed_via:
            commit.append(self._via(net, self.via_lands or end))
            self.armed_via = False
            self.commits.append(commit)
            if self.via_ends_route:
                self.routing = None
            else:
                self.board.layer = kr.OTHER_SIDE[self.board.layer]
                self.routing = (net, end)
            return
        self.commits.append(commit)
        self.routing = None if (target and target.net == net) or joined else (net, end)

    def scroll(self, sx, sy, steps):
        old = self.scale
        self.scale = old * kr.ZOOM_STEP ** steps
        self.pointer = (sx, sy)
        self.offset = (sx - (sx - self.offset[0]) * self.scale / old, sy - (sy - self.offset[1]) * self.scale / old)

    def menu(self, top, item):
        self.menus.append((top, item))
        # Zoom to Fit: the whole 100 x 60 mm board, at (50, 50) to (150, 110).
        self.scale, self.offset = 10.0, (0.0, -300.0)

    def leave_router(self):
        self.key("esc")
        self.key("esc")

    # Inside the stand-in.
    def _pad_at(self, at):
        for candidate in self.board.pads.values():
            if abs(at[0] - candidate.x) <= candidate.half and abs(at[1] - candidate.y) <= candidate.half:
                return candidate
        return None

    def _track(self, net, a, b, layer):
        self.next_id += 1
        item = Item(f"t{self.next_id}", "track", net, layer, a, b, self.track_width)
        self.board.tracks.append(item)
        return item

    def _via(self, net, at):
        self.next_id += 1
        item = Item(f"v{self.next_id}", "via", net, None, at, at, 0.6)
        self.board.tracks.append(item)
        return item

    def _start_router(self):
        at = self.raw()
        start = self._pad_at(at)
        if start and start.name in self.dirty:
            self.strip = self.strip + ["The routing start point violates DRC"]
            return
        if start:
            self.routing = (start.net, (start.x, start.y))
            return
        near = next((i for i in self.board.tracks if i.kind == "via"
                     and abs(i.start[0] - at[0]) < 0.3 and abs(i.start[1] - at[1]) < 0.3), None)
        if near:
            self.routing = (near.net, near.start)
            return
        end = next((point for i in self.board.tracks if i.kind == "track" for point in (i.start, i.end)
                    if abs(point[0] - at[0]) < 0.15 and abs(point[1] - at[1]) < 0.15), None)
        if end:
            self.routing = (next(i.net for i in self.board.tracks if end in (i.start, i.end)), end)


def two_pads(**kw):
    return FakeBoard([pad("R1.1", "/A", 60.0, 80.0, **kw), pad("R2.1", "/A", 70.0, 84.0, **kw),
                      pad("R3.1", "/B", 70.0, 86.0, **kw)])


class Coordinates(unittest.TestCase):
    def test_fit_recovers_scale_and_offset_from_snapped_readings(self):
        snap = lambda v: round(v / 0.25) * 0.25  # noqa: E731
        samples = [(sx, sy, Status(snap((sx - 300) / 20), snap((sy + 1000) / 20), 5.0, 0.25))
                   for sx, sy in ((500, 200), (1500, 250), (900, 700))]
        mapping = kr.fit_mapping(samples, 0.25)
        self.assertAlmostEqual(mapping.ax, 20, delta=0.2)
        sx, sy = mapping.to_screen(60, 80)
        self.assertAlmostEqual(sx, 1500, delta=5)
        self.assertAlmostEqual(sy, 600, delta=5)

    def test_a_misread_digit_fails_the_fit(self):
        samples = [(500, 200, Status(10, 60, 5, 0.25)), (1500, 250, Status(60, 62.5, 5, 0.25)), (900, 700, Status(80, 85, 5, 0.25))]
        with self.assertRaises(RouteFailure) as caught:
            kr.fit_mapping(samples, 0.25)
        self.assertEqual(caught.exception.status, "mapping_failed")

    def test_a_status_bar_that_does_not_move_fails(self):
        samples = [(500, 200, Status(10, 60, 5, 0.25)), (1500, 250, Status(10, 60, 5, 0.25)), (900, 700, Status(10, 60, 5, 0.25))]
        with self.assertRaisesRegex(RouteFailure, "did not change"):
            kr.fit_mapping(samples, 0.25)

    def test_unequal_scales_fail(self):
        samples = [(500, 200, Status(10, 60, 5, 0.25)), (1500, 250, Status(60, 61.25, 5, 0.25)), (900, 700, Status(30, 72.5, 5, 0.25))]
        with self.assertRaisesRegex(RouteFailure, "scales differ"):
            kr.fit_mapping(samples, 0.25)

    def test_a_flipped_view_still_maps(self):
        samples = [(sx, sy, Status((2000 - sx) / 20, sy / 20, 5, 0.25)) for sx, sy in ((500, 200), (1500, 250), (900, 700))]
        mapping = kr.fit_mapping(samples, 0.25)
        self.assertLess(mapping.ax, 0)
        self.assertAlmostEqual(mapping.to_screen(50, 30)[0], 1000, delta=1)

    def test_refine_finds_the_true_offset_from_the_grid_step(self):
        board = two_pads()
        editor = FakeEditor(board, scale=20, offset=(300.0, -1000.0), grid=0.5)
        coarse = Mapping(20, 303.7, 20, -1003.2, 0.3)  # right scale, offsets up to 0.2 mm wrong
        fine = kr.refine_near(editor, coarse, 60.0, 80.0, 0.5)
        sx, sy = fine.to_screen(60.0, 80.0)
        self.assertAlmostEqual(sx, 1500.0, delta=0.8)
        self.assertAlmostEqual(sy, 600.0, delta=0.8)

    def test_zoom_steps_bring_the_points_into_view(self):
        mapping = Mapping(2.0, 800.0, 2.0, 300.0, 0.3)
        points = [(100.0, 50.0), (105.0, 53.0)]
        steps = kr.zoom_steps(mapping, points, SAFE)
        self.assertGreater(steps, 0)
        # Far apart at a close zoom: out.
        self.assertLess(kr.zoom_steps(Mapping(50.0, -1000.0, 50.0, 0.0, 0.1), [(20.0, 10.0), (40.0, 10.0)], SAFE), 0)

    def test_fits_accepts_small_board_zoom_when_all_points_fit_with_room(self):
        # Small 10 mm boards open at ~69 points/mm (> MAX_SCALE 60) with every point well inside SAFE.
        mapping = Mapping(68.8, -6215.0, 68.8, -6735.0, 0.04)
        points = [(103.70, 102.35), (104.25, 106.15)]
        self.assertTrue(kr.fits(mapping, points, (384.0, 155.0, 1613.0, 760.0), 0.15))


class Reading(unittest.TestCase):
    def test_status_bar(self):
        status = kr.parse_status("Z 2.94 X 193.0000 Y 203.0000 dx 193.0000 dy 203.0000 dist 280.1036 grid 0.5000 mm")
        self.assertEqual((status.x, status.y, status.zoom, status.grid), (193.0, 203.0, 2.94, 0.5))

    def test_status_bar_with_what_ocr_gets_wrong(self):
        status = kr.parse_status("Z 2.94 x o.0000 Y O.l000 dx O.0000 dy O.0000 dist O.0000 grid 0.5000 mm")
        self.assertEqual((status.x, status.y, status.zoom, status.grid), (0.0, 0.1, 2.94, 0.5))

    def test_dx_is_not_x(self):
        self.assertIsNone(kr.parse_status("dx 193.0000 dy 203.0000 grid 0.5000 mm"))

    def test_other_units_are_not_read(self):
        self.assertIsNone(kr.parse_status("Z 2.94 X 193.0000 Y 203.0000 grid 0.5000 in"))

    def test_panel(self):
        panel = kr.parse_panel("Routing Track: /J6_D23 Resolved Netclass: Default Corner Style 45-degree Mode Walk around Track Width: 0.2000 mm")
        self.assertEqual((panel.routing, panel.net, panel.mode, panel.width), (True, "/J6_D23", "Walk around", 0.2))
        self.assertFalse(kr.parse_panel("Pads 263 Vias 141 Track Segments 598").routing)

    def test_same_net(self):
        self.assertTrue(kr.same_net("/J6_D23", "/J6_D23"))
        self.assertTrue(kr.same_net("/J6D23", "/J6_D23"))
        self.assertTrue(kr.same_net("ISER_OPTO_A", "/SER_OPTO_A"))
        self.assertFalse(kr.same_net("/J6_D28", "/J6_D23"))
        self.assertTrue(kr.same_net("Net-(R1...", "Net-(R15-Pad1)"))
        self.assertFalse(kr.same_net("Net-(R2...", "Net-(R15-Pad1)"))

    def test_refusal_is_what_appeared(self):
        self.assertEqual(kr.refusal_text(["Zoom 5.95"], ["Zoom 5.95", "The routing start point violates DRC"]),
                         "The routing start point violates DRC")
        self.assertEqual(kr.refusal_text([], ["Q The routing start point violates DRC."]), "The routing start point violates DRC.")
        self.assertIsNone(kr.refusal_text(["Zoom 5.95"], ["Zoom 5.95"]))


class Layers(unittest.TestCase):
    def test_smd_starts_on_its_layer(self):
        self.assertEqual(kr.plan_layers(pad("A.1", "n", 0, 0), pad("B.1", "n", 1, 1), 0, None, "B.Cu"), ["F.Cu"])

    def test_via_takes_it_to_the_other_side(self):
        through = ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")
        self.assertEqual(kr.plan_layers(pad("A.1", "n", 0, 0), pad("B.1", "n", 1, 1, layers=through, through=True), 1, None, "F.Cu"),
                         ["F.Cu", "B.Cu"])

    def test_a_target_the_plan_cannot_reach_is_refused(self):
        with self.assertRaises(RouteFailure) as caught:
            kr.plan_layers(pad("A.1", "n", 0, 0), pad("B.1", "n", 1, 1, layers=("B.Cu",)), 0, None, "F.Cu")
        self.assertEqual(caught.exception.status, "bad_request")

    def test_a_layer_the_pad_is_not_on_is_refused(self):
        with self.assertRaises(RouteFailure):
            kr.plan_layers(pad("A.1", "n", 0, 0), pad("B.1", "n", 1, 1), 0, "B.Cu", "F.Cu")

    def test_through_pad_prefers_the_active_layer(self):
        through = ("F.Cu", "B.Cu")
        self.assertEqual(kr.plan_layers(pad("A.1", "n", 0, 0, layers=through, through=True),
                                        pad("B.1", "n", 1, 1, layers=through, through=True), 0, None, "B.Cu"), ["B.Cu"])


class PadApproaches(unittest.TestCase):
    def test_opto_corridor_has_axis_ends_and_beveled_middle(self):
        start = PadInfo("D2.2", "n", 181.2, 131.1725, ("F.Cu",), 0.35, False, (1.0, 0.7), 270)
        end = PadInfo("U9.2", "n", 184.42, 128.5, ("F.Cu",), 0.71125, False, (1.4225, 2.6), 270)
        points = kr.pad_waypoints(start, end, [], 0.05)
        from kicad_route_quality import validate_polyline
        self.assertEqual(validate_polyline([(start.x, start.y)] + [p[1:] for p in points] + [(end.x, end.y)]), [])
        self.assertEqual(points[0][2], start.y)
        self.assertEqual(points[-1][2], end.y)
        self.assertLess(points[0][1], 182.8)  # before the neighboring optocoupler pad

    def test_quality_keeps_same_origin_errors_and_occurrences(self):
        from kicad_route_quality import Finding
        first = Finding("error", "non_45_segment", "n", 0, (0, 0), "(0,0) -> (1,2)")
        second = dataclasses.replace(first, message="(0,0) -> (2,3)")
        board = kr.KipyBoard.__new__(kr.KipyBoard)
        board.project, board.board = "/board", None
        with patch.object(kr, "audit_live_board", return_value=([first, first, second], {})):
            self.assertEqual(len(board.quality()), 3)

    def test_rotated_rectangular_pad_uses_its_physical_axes_and_size(self):
        start = dataclasses.replace(pad("A.1", "n", 0, 0), size=(1.4, 0.6), angle=90)
        end = pad("B.1", "n", 10, 4)
        self.assertEqual(kr.pad_waypoints(start, end, []),
                         [("through", 3.0, 0.0), ("through", 7.0, 4.0)])

    def test_near_diagonal_pair_gets_a_45_degree_middle_and_axis_ends(self):
        start, end = pad("A.1", "n", 0, 0), pad("B.1", "n", 4, 3)
        waypoints = kr.pad_waypoints(start, end, [])
        from kicad_route_quality import validate_polyline
        self.assertEqual(validate_polyline([(0, 0)] + [p[1:] for p in waypoints] + [(4, 3)]), [])

    @staticmethod
    def led2():
        """LED2.1 with its anode pad (another net) 1.5 mm west, and R17.2 down and west."""
        start = PadInfo("LED2.1", "/LED2_K", 111.915, 111.75, ("F.Cu",), 0.4, False, (0.8, 0.8), 270,
                        others=((110.017, 111.35, 110.817, 112.15),))
        end = PadInfo("R17.2", "/LED2_K", 109.0035, 113.45, ("F.Cu",), 0.40325, False, (0.8065, 0.864), 0,
                      others=((107.093, 113.018, 107.9, 113.882),))
        return start, end

    def test_a_leg_past_the_parts_other_pad_is_planned_the_other_way(self):
        start, end = self.led2()
        points = kr.pad_waypoints(start, end, [], 0.05)
        path = [(start.x, start.y)] + [p[1:] for p in points] + [(end.x, end.y)]
        self.assertEqual(points[0][1], start.x, "LED2.1 leaves straight down, away from its anode pad")
        self.assertTrue(kr.clear_of_others(path, (start, end)))
        from kicad_route_quality import validate_polyline
        self.assertEqual(validate_polyline(path), [])

    def test_without_the_other_pad_the_plan_is_unchanged(self):
        start, end = self.led2()
        bare = kr.pad_waypoints(dataclasses.replace(start, others=()), dataclasses.replace(end, others=()), [], 0.05)
        self.assertEqual(bare[0], ("through", 111.015, 111.75))

    def test_an_escape_toward_a_waypoint_turns_away_from_the_other_pad(self):
        start, end = self.led2()
        planned = kr.pad_waypoints(start, end, [("through", 109.0, 112.6)], 0.05)
        self.assertEqual(planned[0][1], start.x)
        self.assertGreater(planned[0][2], start.y)

    # LED2.1 to R17.2 with --through 111.915,112.5, as KiCad laid it on the Mac: two jogs a few microns long.
    JOGGED = [(111.915, 111.75), (111.915, 112.521121), (111.922184, 112.528305), (110.991212, 113.459277),
              (109.012777, 113.459277), (109.0035, 113.45)]

    def test_jogs_are_trued_onto_45_degree_headings_within_reach(self):
        corners, keep = kr._true_chain(self.JOGGED)
        self.assertEqual(keep, [0, 2, 3])
        from kicad_route_quality import validate_polyline
        self.assertEqual(validate_polyline(corners), [])
        self.assertEqual((corners[0], corners[-1]), (self.JOGGED[0], self.JOGGED[-1]))
        self.assertEqual(len(corners), 4)
        self.assertAlmostEqual(corners[1][0], 111.915, places=6)
        self.assertAlmostEqual(corners[2][1], 113.45, places=6)
        self.assertTrue(all(kr._point_to_polyline(c, self.JOGGED) <= 0.02 for c in corners))

    def test_clean_copper_and_far_jogs_are_left_alone(self):
        self.assertIsNone(kr._true_chain([(0, 0), (0, 1), (1, 2), (3, 2)]))
        # Two fixed ends 0.04 mm off one line: truing would move an end, so the jog stays for the quality check.
        self.assertIsNone(kr._true_chain([(0, 0), (0, 1), (0.04, 1.0), (0.04, 3)]))

    def test_true_up_moves_and_deletes_only_the_new_tracks_and_keeps_fixed_ends(self):
        start = PadInfo("LED2.1", "n", 111.915, 111.75, ("F.Cu",), 0.4, False)
        end = PadInfo("R17.2", "n", 109.0035, 113.45, ("F.Cu",), 0.4, False)
        old = Item("old", "track", "n", "F.Cu", (109.0035, 111.75), (109.0035, 113.45), 0.25)
        added = [Item(f"t{i}", "track", "n", "F.Cu", a, b, 0.25) for i, (a, b) in enumerate(zip(self.JOGGED, self.JOGGED[1:]))]
        moves, deletes, furthest, via_moves = kr.true_up(added, [old], [start, end])
        self.assertEqual(via_moves, {})
        self.assertEqual((len(moves), len(deletes)), (3, 2))
        self.assertNotIn("old", moves)
        self.assertLessEqual(furthest, 0.02)
        ends = sorted(point for a, b in moves.values() for point in (a, b))
        self.assertIn((111.915, 111.75), ends)
        self.assertIn((109.0035, 113.45), ends)

    # ENC1.2 via@204.358,96.4 on the Mac: the new via landed 0.0136 mm off the pad's axis, with a jog into it.
    def test_a_new_via_slides_onto_the_line_with_its_track_and_an_older_via_never_moves(self):
        start = PadInfo("ENC1.2", "GND", 204.358, 94.549, ("F.Cu",), 0.75, False)
        down = Item("a", "track", "GND", "F.Cu", (204.358, 94.549), (204.358, 96.426648), 0.3)
        jog = Item("b", "track", "GND", "F.Cu", (204.358, 96.426648), (204.348385, 96.436263), 0.3)
        via = Item("v", "via", "GND", None, (204.348385, 96.436263), (204.348385, 96.436263), 0.6)
        moves, deletes, furthest, via_moves = kr.true_up([down, jog, via], [], [start])
        self.assertEqual(moves, {"a": ((204.358, 94.549), (204.358, 96.436263))})
        self.assertEqual((deletes, via_moves), (["b"], {"v": (204.358, 96.436263)}))
        self.assertLess(furthest, 0.0137)
        # The same via from before this run stays, and so does the jog onto it.
        self.assertEqual(kr.true_up([down, jog], [via], [start])[:2], ({}, []))

    def test_a_true_up_that_does_not_read_back_stops_the_run(self):
        board = two_pads()
        run = Run(FakeEditor(board), board, Request("R1.1", "R2.1", []))
        track = Item("t", "track", "/A", "F.Cu", (60.0, 80.0), (60.01, 80.01), 0.2)
        board.tracks = [track]
        board.true_up = lambda moves, deletes, vias: None  # the editor ignored the edit
        with self.assertRaises(RouteFailure) as caught:
            run.apply_true_up([track], {}, ["t"])
        self.assertEqual(caught.exception.status, "true_up_failed")
        board.true_up = lambda moves, deletes, vias: board.tracks.clear()
        self.assertEqual(run.apply_true_up([track], {}, ["t"]), [])
        self.assertEqual(run.true_ups, 2)

    def test_leg_to_box_distance(self):
        box = (0.0, 0.0, 1.0, 1.0)
        self.assertEqual(kr._segment_box_distance((-1, 0.5), (2, 0.5), box), 0.0)
        self.assertAlmostEqual(kr._segment_box_distance((-1, 1.3), (2, 1.3), box), 0.3)
        self.assertAlmostEqual(kr._segment_box_distance((2, 2), (3, 3), box), 2 ** 0.5)

    def test_straight_connection_needs_no_extra_corner(self):
        self.assertEqual(kr.pad_waypoints(pad("A.1", "n", 0, 0), pad("B.1", "n", 10, 0), []), [])

    def test_via_order_and_explicit_detour_are_preserved(self):
        points = [("via", 3, 2), ("through", 7, 3), ("via", 8, 3)]
        planned = kr.pad_waypoints(pad("A.1", "n", 0, 0), pad("B.1", "n", 10, 4), points)
        self.assertEqual(planned[1:-1], points)
        self.assertEqual(points[0], ("via", 3, 2))

    def test_diagonal_boundary_crossing_after_an_internal_stub_is_rejected(self):
        endpoint = dataclasses.replace(pad("A.1", "n", 0, 0), size=(2, 1))
        items = [Item("a", "track", "n", "F.Cu", (0, 0), (0.2, 0), 0.2),
                 Item("b", "track", "n", "F.Cu", (0.2, 0), (1.2, 1), 0.2)]
        with self.assertRaises(RouteFailure):
            kr.check_pad_entries([endpoint], items, [])

    def test_old_diagonal_does_not_get_attributed_to_new_straight_route(self):
        endpoint = dataclasses.replace(pad("A.1", "n", 0, 0), size=(2, 1))
        old = Item("old", "track", "n", "F.Cu", (0, 0), (-2, -2), 0.2)
        new = Item("new", "track", "n", "F.Cu", (0, 0), (2, 0), 0.2)
        kr.check_pad_entries([endpoint], [old, new], [old])

    def test_diagonal_crossing_with_both_ends_outside_is_rejected(self):
        endpoint = dataclasses.replace(pad("A.1", "n", 0, 0), size=(2, 1))
        old = Item("old", "track", "n", "F.Cu", (0, 0), (-2, 0), 0.2)
        new = Item("new", "track", "n", "F.Cu", (-2, -2), (2, 2), 0.2)
        with self.assertRaises(RouteFailure):
            kr.check_pad_entries([endpoint], [old, new], [old])


class Closure(unittest.TestCase):
    def track(self, name, a, b, layer="F.Cu", net="/A"):
        return Item(name, "track", net, layer, a, b, 0.2)

    def setUp(self):
        self.a = pad("A.1", "/A", 0, 0)
        self.b = pad("B.1", "/A", 10, 0, layers=("F.Cu", "B.Cu"), through=True)

    def test_a_chain_closes(self):
        self.assertTrue(kr.connected(self.a, self.b, [self.track("1", (0, 0), (5, 0)), self.track("2", (5, 0), (10, 0))]))

    def test_a_gap_does_not(self):
        self.assertFalse(kr.connected(self.a, self.b, [self.track("1", (0, 0), (5, 0)), self.track("2", (6, 0), (10, 0))]))

    def test_crossing_layers_needs_a_via(self):
        items = [self.track("1", (0, 0), (5, 0)), self.track("2", (5, 0), (10, 0), layer="B.Cu")]
        self.assertFalse(kr.connected(self.a, self.b, items))
        items.append(Item("v", "via", "/A", None, (5, 0), (5, 0), 0.6))
        self.assertTrue(kr.connected(self.a, self.b, items))

    def test_another_net_does_not_count(self):
        self.assertFalse(kr.connected(self.a, self.b, [self.track("1", (0, 0), (10, 0), net="/B")]))

    def test_diff_counts(self):
        one, two = self.track("1", (0, 0), (1, 0)), self.track("2", (1, 0), (2, 0))
        via = Item("v", "via", "/A", None, (2, 0), (2, 0), 0.6)
        change = kr.diff_items([one], [two, via])
        self.assertEqual((change["segments_added"], change["vias_added"], change["items_removed"]), (1, 1, 1))

    def test_new_drc_only_near_and_new(self):
        old = {"type": "clearance", "description": "", "items": [{"pos": {"x": 5, "y": 5}}]}
        new_near = {"type": "tracks_crossing", "description": "x", "items": [{"pos": {"x": 6, "y": 6}}]}
        new_far = {"type": "clearance", "description": "", "items": [{"pos": {"x": 90, "y": 90}}]}
        drc = kr.new_drc({"violations": [old], "unconnected_items": [1, 2]},
                         {"violations": [old, new_near, new_far], "unconnected_items": [1]}, (0, 0, 10, 10))
        self.assertEqual([v["type"] for v in drc["new_errors_nearby"]], ["tracks_crossing"])
        self.assertEqual(drc["unconnected"], [2, 1])


class WholeRoute(unittest.TestCase):
    def test_corridor_escapes_both_pads_before_joining_the_tracks(self):
        start = PadInfo("D2.2", "n", 61.2, 81.1725, ("F.Cu",), 0.35, False, (1.0, 0.7), 270)
        end = PadInfo("U9.2", "n", 64.42, 78.5, ("F.Cu",), 0.71125, False, (1.4225, 2.6), 270)
        board = FakeBoard([start, end])
        result, editor = self.go(board, start="D2.2", target="U9.2")
        self.assertTrue(result["ok"], result)
        self.assertEqual(editor.keys.count("x"), 2)
        kr.check_pad_entries([start, end], board.items(), [])

    def go(self, board, editor=None, **request):
        editor = editor or FakeEditor(board)
        result = Run(editor, board, Request(request.pop("start", "R1.1"), request.pop("target", "R2.1"),
                                            request.pop("waypoints", []), **request)).run()
        return result, editor

    def test_routes_and_closes(self):
        board = two_pads()
        result, editor = self.go(board)
        self.assertEqual(result["status"], "closed", result)
        self.assertTrue(result["closed"])
        self.assertGreaterEqual(result["segments_added"], 3)
        self.assertEqual(board.tracks[0].start[1], board.tracks[0].end[1])
        endpoint = board.pad("R2.1")
        self.assertTrue(any(item.start == (endpoint.x, endpoint.y) and item.start[1] == item.end[1]
                            for item in board.tracks))
        self.assertIn("x", editor.keys)
        self.assertEqual(result["picture"], "/tmp/R1.1-R2.1.png")

    def test_a_corner_that_snaps_onto_the_target_ends_the_route_with_no_more_clicks(self):
        board = two_pads()
        target = board.pad("R2.1")
        result, editor = self.go(board, waypoints=[("through", target.x, target.y)])
        self.assertEqual(result["status"], "closed", result)
        self.assertEqual(len(editor.clicks), 3)

    def test_nets_that_differ_touch_nothing(self):
        board = two_pads()
        editor = FakeEditor(board)
        result, _ = self.go(board, editor, target="R3.1")
        self.assertEqual(result["status"], "wrong_net")
        self.assertEqual((editor.keys, editor.clicks), ([], []))

    def test_named_net_that_differs_touches_nothing(self):
        board = two_pads()
        editor = FakeEditor(board)
        result, _ = self.go(board, editor, net="/B")
        self.assertEqual(result["status"], "wrong_net")
        self.assertEqual((editor.keys, editor.clicks), ([], []))

    def test_wrong_net_on_screen_is_caught_before_a_click(self):
        board = FakeBoard([pad("R1.1", "/A", 60.0, 80.0), pad("R2.1", "/A", 70.0, 84.0), pad("R3.1", "/B", 61.0, 80.0)])
        editor = FakeEditor(board)
        result, _ = self.go(board, editor, hover_offset=(1.0, 0.0))
        self.assertEqual(result["status"], "wrong_net", result)
        self.assertEqual(result["shown"], "/B")
        self.assertEqual(editor.clicks, [])

    def test_refused_start_reports_the_routers_words_and_clicks_nothing(self):
        board = two_pads()
        editor = FakeEditor(board, dirty={"R1.1"})
        result, _ = self.go(board, editor)
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["refusal"], "The routing start point violates DRC")
        self.assertEqual(editor.clicks, [])
        self.assertEqual(board.tracks, [])

    def test_a_view_that_moved_stops_before_clicking_there(self):
        board = two_pads()
        editor = FakeEditor(board)
        editor.shift_after_clicks = 1  # the first click pans the view by 10 mm
        result, _ = self.go(board, editor, waypoints=[("through", 65.0, 82.0)])
        self.assertEqual(result["status"], "mapping_failed", result)
        self.assertEqual(len(editor.clicks), 1)
        self.assertEqual(board.tracks, [], "the partial route is undone")
        self.assertTrue(result["undone"])

    def test_an_unreadable_status_bar_clicks_nothing(self):
        board = two_pads()
        editor = FakeEditor(board)
        editor.garble_status = True
        result, _ = self.go(board, editor)
        self.assertEqual(result["status"], "mapping_failed")
        self.assertEqual((editor.keys.count("x"), editor.clicks), (0, []))

    def test_via_that_ends_the_route_restarts_from_the_via(self):
        through = ("F.Cu", "B.Cu")
        board = FakeBoard([pad("R1.1", "/A", 60.0, 80.0), pad("J1.1", "/A", 70.0, 84.0, layers=through, through=True)])
        result, editor = self.go(board, target="J1.1", waypoints=[("via", 65.0, 82.0)])
        self.assertEqual(result["status"], "closed", result)
        self.assertEqual(result["vias_added"], 1)
        self.assertGreaterEqual(result["segments_added"], 3)
        self.assertEqual(editor.keys.count("x"), 2)
        self.assertEqual(board.layer, "B.Cu")

    def test_a_via_that_lands_far_from_its_point_stops_and_undoes_the_route(self):
        through = ("F.Cu", "B.Cu")
        board = FakeBoard([pad("R1.1", "/A", 60.0, 80.0), pad("J1.1", "/A", 70.0, 84.0, layers=through, through=True)])
        editor = FakeEditor(board)
        editor.via_lands = (61.0, 80.5)
        result, _ = self.go(board, editor, target="J1.1", waypoints=[("via", 65.0, 82.0)])
        self.assertEqual(result["status"], "via_missed", result)
        self.assertIn("landed at (61.00, 80.50)", result["reason"])
        self.assertTrue(result["undone"])
        self.assertEqual(board.tracks, [])

    def test_a_plain_call_in_an_editor_left_in_shove_routes_in_walk_around(self):
        board = two_pads()
        editor = FakeEditor(board)
        editor.router_mode = "Shove"
        result, _ = self.go(board, editor)
        self.assertEqual(result["status"], "closed", result)
        self.assertEqual(editor.modes_set, ["Walk around"])
        self.assertEqual((result["router_mode"], result["mode_changes"]), ("Walk around", ["Walk around"]))
        self.assertEqual(editor.keys.count("x"), 3, "armed, left to set the mode, armed again, then the second pad")

    def test_a_plain_call_in_walk_around_changes_no_mode(self):
        board = two_pads()
        result, editor = self.go(board)
        self.assertEqual(result["status"], "closed", result)
        self.assertEqual((editor.modes_set, result["router_mode"]), ([], "Walk around"))

    def test_shove_is_set_proved_from_the_panel_and_put_back(self):
        board = two_pads()
        result, editor = self.go(board, mode="shove")
        self.assertEqual(result["status"], "closed", result)
        self.assertEqual(editor.modes_set, ["Shove", "Walk around"])
        self.assertEqual((result["router_mode"], editor.router_mode), ("Shove", "Walk around"))

    def test_shove_is_put_back_when_the_route_fails(self):
        board = two_pads()
        editor = FakeEditor(board)
        editor.shift_after_clicks = 1  # the first click pans the view: the run stops mid-route
        result, _ = self.go(board, editor, mode="shove", waypoints=[("through", 65.0, 82.0)])
        self.assertEqual(result["status"], "mapping_failed", result)
        self.assertEqual(editor.modes_set, ["Shove", "Walk around"])
        self.assertEqual((result["router_mode"], editor.router_mode), ("Shove", "Walk around"))
        self.assertTrue(result["undone"])

    def test_shove_is_put_back_when_leaving_the_router_raises(self):
        board = two_pads()
        editor = FakeEditor(board)
        board.wait_ready = lambda: (_ for _ in ()).throw(RouteFailure("ipc_failed", "KiCad stayed busy"))
        result, _ = self.go(board, editor, mode="shove")
        self.assertFalse(result["ok"], result)
        self.assertEqual(editor.modes_set[-1], "Walk around")

    def test_a_mode_that_did_not_take_stops_the_run(self):
        board = two_pads()
        editor = FakeEditor(board)
        editor.mode_sticks = False
        result, _ = self.go(board, editor, mode="shove")
        self.assertEqual(result["status"], "mode_not_set", result)
        self.assertEqual(editor.clicks, [])
        self.assertEqual(result["router_mode"], "Walk around")

    def test_width_is_set_proved_from_the_panel_and_applied_to_copper(self):
        board = two_pads()
        result, editor = self.go(board, width=0.5)
        self.assertEqual(result["status"], "closed", result)
        self.assertEqual(editor.widths_set, [0.5])
        self.assertEqual(result["width_mm"], 0.5)
        self.assertEqual(result["width_changes"], [0.5])
        self.assertTrue(all(track.width == 0.5 for track in board.tracks))

    def test_matching_width_does_not_open_dimensions_dialog(self):
        board = two_pads()
        result, editor = self.go(board, width=0.2)
        self.assertEqual(result["status"], "closed", result)
        self.assertEqual(editor.widths_set, [])
        self.assertEqual(result["width_mm"], 0.2)
        self.assertNotIn("width_changes", result)

    def test_a_width_that_did_not_take_stops_the_run(self):
        board = two_pads()
        editor = FakeEditor(board)
        editor.width_sticks = False
        result, _ = self.go(board, editor, width=0.5)
        self.assertEqual(result["status"], "width_not_set", result)
        self.assertEqual(editor.clicks, [])
        self.assertEqual(result["width_mm"], 0.2)

    def test_mid_route_width_at_switches_width_at_waypoint_and_preserves_both_widths(self):
        start = PadInfo("SP2.1", "/MOTOR_B", 106.30, 102.35, ("F.Cu",), 0.75, False, (1.5, 2.1), 0)
        end = PadInfo("J1.4", "/MOTOR_B", 105.75, 106.15, ("F.Cu",), 0.15, False, (0.3, 1.0), 0)
        board = FakeBoard([start, end])
        editor = FakeEditor(board, scale=25.0, offset=(-1600.0, -2100.0), grid=0.05)
        result, _ = self.go(board, editor, start="SP2.1", target="J1.4", width=0.5,
                            waypoints=[("width_at", 105.75, 105.55, 0.3)])
        self.assertEqual(result["status"], "closed", result)
        self.assertEqual(editor.widths_set, [0.5, 0.3])
        self.assertEqual(result["width_changes"], [0.5, 0.3])
        widths = {round(t.width, 3) for t in board.tracks}
        self.assertEqual(widths, {0.5, 0.3})
        # Final segment into J1.4 must carry the 0.30 mm neck width.
        into_j1 = [t for t in board.tracks if (105.75, 106.15) in (t.start, t.end)]
        self.assertTrue(into_j1 and all(abs(t.width - 0.3) < 1e-6 for t in into_j1))

    def test_true_up_removes_jog_at_collinear_width_transition_and_keeps_both_widths(self):
        start = PadInfo("SP2.1", "/MOTOR_B", 106.30, 102.35, ("F.Cu",), 0.75, False, (1.5, 2.1), 0)
        end = PadInfo("J1.4", "/MOTOR_B", 105.75, 106.15, ("F.Cu",), 0.15, False, (0.3, 1.0), 0)
        added = [
            Item("t1", "track", "/MOTOR_B", "F.Cu", (106.30, 102.35), (106.30, 104.00), 0.50),
            Item("t2", "track", "/MOTOR_B", "F.Cu", (106.30, 104.00), (105.752, 104.548), 0.50),
            Item("t3", "track", "/MOTOR_B", "F.Cu", (105.752, 104.548), (105.752, 105.548), 0.50),
            Item("jog", "track", "/MOTOR_B", "F.Cu", (105.752, 105.548), (105.750, 105.550), 0.30),
            Item("t4", "track", "/MOTOR_B", "F.Cu", (105.750, 105.550), (105.750, 106.150), 0.30),
        ]
        moves, deletes, _, _ = kr.true_up(added, [], [start, end])
        self.assertEqual(deletes, ["jog"])
        self.assertIn("t3", moves)
        self.assertIn("t4", moves)
        # Both t3 (0.50 mm) and t4 (0.30 mm) stay on x=105.750 and meet cleanly.
        self.assertAlmostEqual(moves["t3"][1][0], 105.750, places=5)
        self.assertAlmostEqual(moves["t4"][0][0], 105.750, places=5)
        self.assertEqual(moves["t3"][1], moves["t4"][0])

    def test_a_via_is_compared_with_every_point_asked_for(self):
        board = two_pads()
        run = Run(FakeEditor(board), board, Request("R1.1", "R2.1", [("via", 65.0, 82.0), ("through", 66.0, 82.0), ("via", 68.0, 83.0)]))
        run.grid = 0.5
        via = lambda x, y: Item("v", "via", "/A", None, (x, y), (x, y), 0.6)  # noqa: E731
        self.assertIsNone(run.via_miss([via(65.2, 82.2)]))  # a leg late, it still counts for the first point
        self.assertIsNone(run.via_miss([via(68.2, 83.1)]))
        self.assertIn("landed at (66.50, 86.00)", run.via_miss([via(66.5, 86.0)]))

    def test_a_via_a_grid_step_off_carries_on(self):
        through = ("F.Cu", "B.Cu")
        board = FakeBoard([pad("R1.1", "/A", 60.0, 80.0), pad("J1.1", "/A", 70.0, 84.0, layers=through, through=True)])
        editor = FakeEditor(board)
        editor.via_lands = (65.25, 82.25)
        result, _ = self.go(board, editor, target="J1.1", waypoints=[("via", 65.0, 82.0)])
        self.assertNotEqual(result["status"], "via_missed", result)  # the stand-in's next leg starts from the point asked, so it goes no further
        self.assertEqual(editor.keys.count("x"), 2)

    def test_via_that_carries_on_needs_no_second_x(self):
        through = ("F.Cu", "B.Cu")
        board = FakeBoard([pad("R1.1", "/A", 60.0, 80.0), pad("J1.1", "/A", 70.0, 84.0, layers=through, through=True)])
        editor = FakeEditor(board, via_ends_route=False)
        result, _ = self.go(board, editor, target="J1.1", waypoints=[("via", 65.0, 82.0)])
        self.assertEqual(result["status"], "closed", result)
        self.assertEqual(editor.keys.count("x"), 1)

    def test_a_route_that_does_not_reach_is_undone(self):
        board = FakeBoard([pad("R1.1", "/A", 60.0, 80.0), pad("R2.1", "/A", 70.0, 84.0)])
        editor = FakeEditor(board)
        click = editor.click

        def miss_the_target(sx, sy):  # the router stops short: the click fixes a leg, and it is still routing
            click(sx, sy)
            if editor.routing is None and board.tracks:
                editor.routing = ("/A", board.tracks[-1].end)
                board.tracks[-1].end = (65.0, 80.0)

        editor.click = miss_the_target
        result, _ = self.go(board, editor)
        self.assertEqual(result["status"], "not_closed", result)
        self.assertEqual(board.tracks, [])
        self.assertTrue(result["undone"])
        self.assertEqual((result["segments_added"], result["vias_added"]), (0, 0))

    def test_already_connected_touches_nothing(self):
        board = two_pads()
        board.tracks.append(Item("t", "track", "/A", "F.Cu", (60.0, 80.0), (70.0, 84.0), 0.2))
        editor = FakeEditor(board)
        result, _ = self.go(board, editor)
        self.assertEqual(result["status"], "already_connected")
        self.assertEqual(editor.keys, [])

    def test_a_close_view_is_used_as_it_is(self):
        board = two_pads()
        editor = FakeEditor(board, scale=30, offset=(-1000.0, -2000.0))
        result, _ = self.go(board, editor)
        self.assertEqual(result["status"], "closed", result)
        self.assertEqual(editor.menus, [])
        self.assertNotIn("f4", editor.keys)

    def test_frames_when_the_pads_are_off_screen(self):
        board = two_pads()
        editor = FakeEditor(board, scale=20, offset=(3000.0, 3000.0))
        result, _ = self.go(board, editor)
        self.assertEqual(result["status"], "closed", result)
        self.assertEqual(editor.menus, [("View", "Zoom to Fit")])
        self.assertGreater(result["scale_points_per_mm"], 15)

    def test_hops_there_when_fit_leaves_the_connection_off_screen(self):
        board = two_pads()
        editor = FakeEditor(board, scale=20, offset=(3000.0, 3000.0))
        menu = editor.menu

        def fit_off_to_one_side(top, item):  # seen after File > Revert: the board's middle stays off the canvas
            menu(top, item)
            editor.offset = (-300.0, -900.0)

        editor.menu = fit_off_to_one_side
        result, _ = self.go(board, editor)
        self.assertEqual(result["status"], "closed", result)

    def test_a_second_route_in_the_same_view_does_not_calibrate(self):
        board = two_pads()
        editor = FakeEditor(board)
        self.go(board, editor)
        board.tracks.clear()
        calls = []
        read_at = editor.read_at
        editor.read_at = lambda sx, sy: (calls.append((sx, sy)), read_at(sx, sy))[1]
        result, _ = self.go(board, editor)
        self.assertEqual(result["status"], "closed", result)
        self.assertEqual(len(calls), 4)  # first pad, guide, second pad, join; no calibration probes

    def test_router_ignoring_approaches_is_undone_before_save(self):
        board = two_pads()
        editor = FakeEditor(board)
        click = editor.click
        endpoint = board.pad("R2.1")
        editor.click = lambda sx, sy: click(editor.scale * endpoint.x + editor.offset[0],
                                           editor.scale * endpoint.y + editor.offset[1])
        result, _ = self.go(board, editor, save=True)
        self.assertEqual(result["status"], "pad_entry", result)
        self.assertTrue(result["undone"])
        self.assertEqual(board.tracks, [])
        self.assertFalse(board.saved)

    def test_failed_undo_reports_the_remaining_copper(self):
        board = two_pads()
        editor = FakeEditor(board)
        click, key = editor.click, editor.key
        endpoint = board.pad("R2.1")
        editor.click = lambda sx, sy: click(editor.scale * endpoint.x + editor.offset[0],
                                           editor.scale * endpoint.y + editor.offset[1])
        editor.key = lambda name, command=False: None if name == "z" else key(name, command)
        result, _ = self.go(board, editor, save=True)
        self.assertFalse(result["undone"])
        self.assertEqual(result["segments_added"], len(board.tracks))
        self.assertTrue(result["closed"])
        self.assertFalse(board.saved)

    def test_new_route_quality_error_is_undone_before_save(self):
        board = two_pads()
        board.quality = lambda: {"reverse": "error: acute_or_reversing_corner"} if board.tracks else {}
        result, _ = self.go(board, save=True)
        self.assertEqual(result["status"], "route_quality")
        self.assertTrue(result["undone"])
        self.assertEqual(board.tracks, [])
        self.assertFalse(board.saved)

    def test_warning_only_route_quality_keeps_copper_for_independent_review(self):
        board = two_pads()
        warning = "warning: micro_segment at (104.25, 105.52): 0.0031 mm"
        board.quality = lambda: {("warning", "micro_segment"): warning} if board.tracks else {}
        result, _ = self.go(board, save=False)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "closed")
        self.assertEqual(result["warnings"], [warning])
        self.assertTrue(board.tracks)

    def test_mixed_error_and_warning_still_undoes_copper(self):
        board = two_pads()
        board.quality = lambda: (
            {("warning", "micro_segment"): "warning: micro_segment at (1, 2): 0.01 mm",
             ("error", "hard_90_corner"): "error: hard_90_corner at (1, 2): directions [(1, -1), (1, 1)]"}
            if board.tracks else {}
        )
        result, _ = self.go(board, save=False)
        self.assertEqual(result["status"], "route_quality")
        self.assertTrue(result["undone"])
        self.assertEqual(board.tracks, [])

    def test_post_route_audit_exception_attempts_undo(self):
        board = two_pads()
        def quality():
            if board.tracks:
                raise RuntimeError("lost IPC")
            return {}
        board.quality = quality
        result, _ = self.go(board, save=True)
        self.assertEqual(result["status"], "route_quality_failed")
        self.assertIn("lost IPC", result["reason"])
        self.assertTrue(result["undone"])
        self.assertEqual(board.tracks, [])
        self.assertFalse(board.saved)

    def test_ipc_read_failure_after_first_escape_attempts_undo(self):
        board = two_pads()
        read, failed = board.items, []
        def items():
            if board.tracks and not failed:
                failed.append(True)
                raise RuntimeError("lost IPC after first escape")
            return read()
        board.items = items
        result, editor = self.go(board, save=True)
        self.assertEqual(result["status"], "operation_failed")
        self.assertTrue(result["undone"])
        self.assertGreater(editor.undone, 0)
        self.assertEqual(board.tracks, [])
        self.assertFalse(board.saved)

    def test_failure_after_save_does_not_claim_disk_was_restored(self):
        board = two_pads()
        editor = FakeEditor(board)
        editor.title_clean = lambda: (_ for _ in ()).throw(RuntimeError("title read failed"))
        result, _ = self.go(board, editor, save=True)
        self.assertFalse(result["ok"])
        self.assertTrue(board.saved)
        self.assertTrue(board.tracks)
        self.assertEqual(editor.undone, 0)
        self.assertIsNone(result["saved"])
        self.assertEqual(result["disk_state"], "unverified")
        self.assertNotIn("undone", result)

    def test_save_only_after_closing(self):
        board = two_pads()
        result, _ = self.go(board, save=True)
        self.assertTrue(board.saved and result["saved"])
        board = two_pads()
        result, _ = self.go(board, FakeEditor(board, dirty={"R1.1"}), save=True)
        self.assertFalse(board.saved or result["saved"])


class EndOnCopper(unittest.TestCase):
    """@X,Y: a route that ends on a via or a track end of the start pad's net."""

    def board(self, *items):
        return FakeBoard([pad("R1.1", "/A", 60.0, 80.0), pad("R3.1", "/B", 70.0, 86.0)], items)

    def go(self, board, target, **request):
        editor = FakeEditor(board)
        return Run(editor, board, Request("R1.1", target, request.pop("waypoints", []), **request)).run(), editor

    def test_a_route_closes_on_a_via_of_its_net(self):
        via = Item("v0", "via", "/A", None, (70.0, 84.0), (70.0, 84.0), 0.6)
        board = self.board(via)
        result, _ = self.go(board, "@70,84")
        self.assertEqual(result["status"], "closed", result)
        self.assertEqual(result["to_copper"], "the via at (70.0, 84.0)")
        self.assertIn(via, board.tracks)

    def test_a_route_closes_on_a_track_end_and_leaves_the_track_as_it_was(self):
        old = Item("old", "track", "/A", "F.Cu", (70.0, 84.0), (72.0, 84.0), 0.25)
        board = self.board(old)
        result, _ = self.go(board, "@70.002,84")
        self.assertEqual(result["status"], "closed", result)
        self.assertEqual(result["to_copper"], "the track end at (70.0, 84.0)")
        self.assertIn(old, board.tracks)
        self.assertEqual(result["items_removed"], 0)

    def test_copper_of_another_net_is_refused_before_anything_moves(self):
        result, editor = self.go(self.board(Item("b", "via", "/B", None, (70.0, 84.0), (70.0, 84.0), 0.6)), "@70,84")
        self.assertEqual(result["status"], "bad_request", result)
        self.assertIn("is on /B", result["reason"])
        self.assertEqual((editor.keys, editor.clicks), ([], []))

    def test_a_corner_is_refused_and_a_straight_meeting_is_taken(self):
        down = Item("d", "track", "/A", "F.Cu", (71.0, 83.0), (70.0, 84.0), 0.25)
        west = Item("w", "track", "/A", "F.Cu", (70.0, 84.0), (66.0, 84.0), 0.25)
        east = Item("e", "track", "/A", "F.Cu", (72.0, 84.0), (70.0, 84.0), 0.25)
        start = pad("R1.1", "/A", 60.0, 80.0)
        with self.assertRaises(RouteFailure) as caught:
            kr.copper_end([down, west], (70.0, 84.0), start)
        self.assertIn("corner", caught.exception.reason)
        self.assertEqual(kr.copper_end([east, west], (70.0, 84.0), start).name, "the track end at (70.0, 84.0)")

    def test_the_middle_of_a_track_is_refused(self):
        result, editor = self.go(self.board(Item("t", "track", "/A", "F.Cu", (68.0, 84.0), (72.0, 84.0), 0.25)), "@70,84")
        self.assertEqual(result["status"], "bad_request", result)
        self.assertIn("not at its end", result["reason"])
        self.assertEqual(editor.keys, [])

    def test_nothing_there_or_no_point_is_refused(self):
        result, _ = self.go(self.board(), "@70,84")
        self.assertIn("no via centre or track end", result["reason"])
        result, _ = self.go(self.board(), "@seventy")
        self.assertEqual(result["status"], "bad_request", result)

    def test_copper_already_reached_is_not_routed_again(self):
        board = self.board(Item("t", "track", "/A", "F.Cu", (60.0, 80.0), (70.0, 80.0), 0.25))
        result, editor = self.go(board, "@70,80")
        self.assertEqual(result["status"], "already_connected", result)
        self.assertEqual(editor.clicks, [])

    def test_removed_copper_names_what_is_gone_and_tolerates_a_merge(self):
        old = Item("old", "track", "/A", "F.Cu", (70.0, 84.0), (72.0, 84.0), 0.25)
        via = Item("v", "via", "/A", None, (72.0, 84.0), (72.0, 84.0), 0.6)
        merged = Item("m", "track", "/A", "F.Cu", (68.0, 84.0), (72.0, 84.0), 0.25)
        self.assertEqual(kr.removed_copper([old, via], [merged, via], "/A"), [])
        self.assertEqual(kr.removed_copper([old, via], [via], "/A"), ["the F.Cu track (70.0, 84.0) to (72.0, 84.0)"])
        self.assertEqual(kr.removed_copper([old, via], [old], "/A"), ["the via at (72.0, 84.0)"])

    def test_the_target_has_no_pad_entry_check_but_the_start_pad_does(self):
        start = dataclasses.replace(pad("R1.1", "/A", 60.0, 80.0), size=(0.8, 0.8))
        end = kr.copper_end([Item("v", "via", "/A", None, (70.0, 84.0), (70.0, 84.0), 0.6)], (70.0, 84.0), start)
        self.assertTrue(end.round)
        diagonal = [Item("d", "track", "/A", "F.Cu", (60.0, 80.0), (61.0, 81.0), 0.25),
                    Item("e", "track", "/A", "F.Cu", (61.0, 81.0), (70.0, 84.0), 0.25)]
        with self.assertRaises(RouteFailure):
            kr.check_pad_entries([start, end], diagonal, [])


class WhichPad(unittest.TestCase):
    """SW1 has two pads numbered 1: a spec must say which."""
    SW1 = [pad("SW1.1", "/BTN_1", 104.865, 101.11), pad("SW1.1", "/BTN_1", 117.365, 101.11)]

    def test_several_pads_of_one_number_with_no_point_are_refused_with_their_positions(self):
        with self.assertRaises(RouteFailure) as caught:
            kr.choose_pad("SW1.1", None, self.SW1)
        self.assertEqual(caught.exception.status, "bad_request")
        self.assertIn("(104.865, 101.11), (117.365, 101.11)", caught.exception.reason)

    def test_a_point_picks_the_pad_and_a_far_point_is_refused(self):
        self.assertEqual(kr.choose_pad("SW1.1", (117.365, 101.11), self.SW1).x, 117.365)
        self.assertEqual(kr.choose_pad("SW1.1", (104.87, 101.1), self.SW1).x, 104.865)
        with self.assertRaises(RouteFailure):
            kr.choose_pad("SW1.1", (110.0, 101.11), self.SW1)

    def test_specs(self):
        self.assertEqual(kr.split_pad_spec("SW1.1@117.365,101.11"), ("SW1.1", (117.365, 101.11)))
        self.assertEqual(kr.split_pad_spec("R15.1"), ("R15.1", None))
        for bad in ("R15", "R15.", "SW1.1@x"):
            with self.assertRaises(RouteFailure):
                kr.split_pad_spec(bad)


class EndOnNewVia(unittest.TestCase):
    """via@X,Y: route from the pad to a new via there, and stop."""

    def test_a_route_ends_on_a_new_via_of_its_net(self):
        board = FakeBoard([pad("R1.1", "/A", 60.0, 80.0)])
        editor = FakeEditor(board)
        result = Run(editor, board, Request("R1.1", "via@60,82", [])).run()
        self.assertEqual(result["status"], "closed", result)
        self.assertEqual(result["vias_added"], 1)
        self.assertEqual(result["vias_at"], [[60.0, 82.0]])
        self.assertIn("design check", result["plane"])
        self.assertEqual(sum(i.kind == "via" for i in board.tracks), 1)

    def test_a_via_spot_on_a_pad_or_other_copper_is_refused_before_anything_moves(self):
        start = pad("R1.1", "/A", 60.0, 80.0)
        boxes = [("R1.1", "/A", (59.6, 79.6, 60.4, 80.4)), ("R2.1", "/B", (62.6, 79.6, 63.4, 80.4))]
        other = Item("t", "track", "/B", "F.Cu", (58.0, 83.0), (62.0, 83.0), 0.25)
        own = Item("o", "track", "/A", "F.Cu", (58.0, 85.0), (62.0, 85.0), 0.25)
        for spot, words in (((60.0, 80.1), "inside R1.1"), ((62.3, 80.0), "from R2.1"), ((60.0, 82.6), "/B track"),
                            ((60.0, 85.0), "end there with @X,Y")):
            with self.assertRaises(RouteFailure) as caught:
                kr.check_via_spot(spot, start, [other, own], boxes)
            self.assertIn(words, caught.exception.reason)
        kr.check_via_spot((60.0, 81.5), start, [other, own], boxes)  # clear of everything

    def test_a_refused_via_spot_touches_nothing(self):
        board = FakeBoard([pad("R1.1", "/A", 60.0, 80.0), pad("R2.1", "/B", 61.0, 80.0)])
        editor = FakeEditor(board)
        result = Run(editor, board, Request("R1.1", "via@61,80", [])).run()
        self.assertEqual(result["status"], "bad_request", result)
        self.assertEqual((editor.keys, editor.clicks), ([], []))


class NetReading(unittest.TestCase):
    """The router's panel read by OCR, against the board's own net names."""
    NETS = ["/D14_GPIO", "/D16_ADC1", "/D17_ADC2", "/D18_ADC3", "/D19_ADC4", "/D28_ADC11", "/J6_3V3", "/J6_D14",
            "/J6_D16", "/J6_D17", "/J6_D18", "/J6_D19", "/J6_D28", "GND", "3V3_D"]

    def test_a_misread_name_is_taken_as_the_one_board_net_clearly_nearest(self):
        # U8.35 on 2026-11-10: the panel showed /D28_ADC11 and OCR read "ID28JDC11".
        ok, nearest = kr.read_net("ID28JDC11", "/D28_ADC11", self.NETS)
        self.assertTrue(ok)
        self.assertEqual(nearest[0], "/D28_ADC11 (1)")

    def test_a_reading_of_another_net_still_stops(self):
        ok, nearest = kr.read_net("ID18JDC3", "/D28_ADC11", self.NETS)
        self.assertFalse(ok)
        self.assertEqual(nearest[0], "/D18_ADC3 (1)")
        self.assertFalse(kr.read_net("/J6_D28", "/D28_ADC11", self.NETS)[0])

    def test_two_nets_equally_near_or_no_list_still_stop(self):
        self.assertFalse(kr.read_net("ID1JADC1", "/D16_ADC1", self.NETS)[0])  # /D16_ADC1 and /D18_ADC3 both near
        self.assertFalse(kr.read_net("ID28JDC11", "/D28_ADC11", [])[0])

    # C1.2 L1.1 on 2026-11-11: the panel showed 3V3_A and OCR read "3V3J", one letter from 3V3_A and from 3V3_D.
    def test_a_misread_supply_passes_when_only_it_and_ground_are_near(self):
        ok, nearest = kr.read_net("3V3J", "3V3_A", ["3V3_A", "GND"], self.NETS + ["3V3_A"])
        self.assertTrue(ok)
        self.assertEqual(nearest[0], "3V3_A (1)")

    def test_a_misread_supply_stops_with_the_other_supply_near(self):
        ok, nearest = kr.read_net("3V3J", "3V3_A", ["3V3_A", "3V3_D", "GND"], self.NETS + ["3V3_A"])
        self.assertFalse(ok)
        self.assertEqual(nearest[:2], ["3V3_A (1)", "3V3_D (1)"])

    def test_a_reading_of_a_nearby_net_stops(self):
        self.assertFalse(kr.read_net("3V3_D", "3V3_A", ["3V3_A", "3V3_D"])[0])
        self.assertFalse(kr.read_net("GND", "3V3_A", ["3V3_A", "GND"])[0])

    def test_a_reading_that_is_exactly_another_boards_net_stops_even_when_it_is_not_near(self):
        ok, nearest = kr.read_net("3V3_D", "3V3_A", ["3V3_A", "GND"], self.NETS + ["3V3_A"])
        self.assertFalse(ok)
        self.assertIn("3V3_D (0, not near)", nearest)

    def test_alone_near_a_reading_within_the_tolerance_passes_and_a_far_one_stops(self):
        self.assertTrue(kr.read_net("3V3J", "3V3_A", ["3V3_A"])[0])
        self.assertFalse(kr.read_net("GNDX", "3V3_A", ["3V3_A"])[0])

    def test_the_near_nets_are_the_copper_within_reach_on_any_layer(self):
        boxes = [("C1.2", "3V3_A", (116.0, 94.2, 116.7, 94.8)), ("C1.1", "GND", (114.9, 94.2, 115.7, 94.8)),
                 ("U2.1", "3V3_D", (119.0, 94.0, 119.5, 95.0))]
        items = [Item("t", "track", "3V3_A", "F.Cu", (115.7, 93.0), (117.2, 94.5), 0.4),
                 Item("v", "via", "/SD_CLK", None, (116.35, 95.9), (116.35, 95.9), 0.6),
                 Item("b", "track", "/BTN_1", "B.Cu", (110.0, 96.4), (113.0, 96.4), 0.25)]
        self.assertEqual(kr.near_nets((116.345, 94.5), items, boxes, 0.7), ["3V3_A", "GND"])
        self.assertEqual(kr.near_nets((116.345, 94.5), items, boxes, 1.2), ["/SD_CLK", "3V3_A", "GND"])
        self.assertEqual(kr.near_nets((116.345, 94.5), items, boxes, 2.8), ["/SD_CLK", "3V3_A", "3V3_D", "GND"])

    def test_the_route_stops_on_a_misread_supply_beside_the_other_supply(self):
        board = FakeBoard([pad("C1.2", "3V3_A", 60.0, 80.0), pad("L1.1", "3V3_A", 62.0, 80.0),
                           pad("U2.1", "3V3_D", 60.0, 80.9)])
        board.net_names = lambda: ["3V3_A", "3V3_D", "GND"]
        editor = FakeEditor(board)
        real = editor.read_panel
        editor.read_panel = lambda: dataclasses.replace(real(), net="3V3J") if editor.routing else real()
        result = Run(editor, board, Request("C1.2", "L1.1", [])).run()
        self.assertEqual(result["status"], "wrong_net", result)
        self.assertIn("3V3_D", result["near_nets"])
        self.assertIn("3V3_D (1)", result["reason"])
        del board.pads["U2.1"]
        board.tracks = []
        editor = FakeEditor(board)
        real = editor.read_panel
        editor.read_panel = lambda: dataclasses.replace(real(), net="3V3J") if editor.routing else real()
        result = Run(editor, board, Request("C1.2", "L1.1", [])).run()
        self.assertEqual(result["status"], "closed", result)
        self.assertEqual(result["net_readings"][0]["near"], ["3V3_A"])

    def test_the_route_reports_a_reading_taken_as_the_nearest_net(self):
        board = FakeBoard([pad("R1.1", "/D28_ADC11", 60.0, 80.0), pad("R2.1", "/D28_ADC11", 70.0, 84.0)])
        board.net_names = lambda: self.NETS
        editor = FakeEditor(board)
        real = editor.read_panel
        editor.read_panel = lambda: dataclasses.replace(real(), net="ID28JDC11") if editor.routing else real()
        result = Run(editor, board, Request("R1.1", "R2.1", [])).run()
        self.assertEqual(result["status"], "closed", result)
        self.assertEqual(result["net_readings"][0]["shown"], "ID28JDC11")


class PanelColon(unittest.TestCase):
    def test_a_colon_read_as_marks_is_not_taken_for_the_net(self):
        # SW1.1 on 2026-11-11: OCR read "Routing Track: /BTN_1" with the colon as ".'" and the slash as I.
        text = ("Routing Track.' IBTN_1 Corner Style Mode Track Width: 0.2500 mm Min Clearance: 0.2000 mm Resolved "
                "Netclass: Default 45-degree Walk around (from netclass 'Default') (from netclass 'Default')")
        panel = kr.parse_panel(text)
        self.assertEqual((panel.routing, panel.net, panel.mode, panel.width), (True, "IBTN_1", "Walk around", 0.25))
        self.assertTrue(kr.same_net(panel.net, "/BTN_1"))
        self.assertEqual(kr.parse_panel("Routing Track /J6_D23 Mode").net, "/J6_D23")
        self.assertEqual(kr.parse_panel("Routing Track: /J6_D23 Mode").net, "/J6_D23")


class Gap(unittest.TestCase):
    def test_a_route_that_did_not_close_says_where_each_half_stopped(self):
        # R36.1 @122.317369,105.912631 on 2026-11-11: the halves stopped 1.55 mm apart.
        start = PadInfo("R36.1", "/AMP_EN", 119.9, 103.3535, ("F.Cu",), 0.4, False)
        end = PadInfo("the track end at (122.317369, 105.912631)", "/AMP_EN", 122.317369, 105.912631, ("F.Cu",), 0.125,
                      False, round=True)
        t = lambda i, a, b: Item(i, "track", "/AMP_EN", "F.Cu", a, b, 0.25)  # noqa: E731
        items = [t("a", (119.9, 103.3535), (119.92875, 103.3535)), t("b", (119.92875, 103.3535), (120.620567, 104.045317)),
                 t("c", (120.620567, 104.045317), (120.620567, 104.275)),
                 t("d", (122.317369, 105.912631), (122.229738, 105.825)), t("e", (122.229738, 105.825), (120.620567, 105.825)),
                 Item("x", "track", "/B", "F.Cu", (120.620567, 104.5), (120.620567, 105.5), 0.25)]
        words = kr.gap_words(start, end, items)
        self.assertIn("stops at (120.621, 104.275)", words)
        self.assertIn("at (120.621, 105.825), 1.55 mm apart", words)


class LandOnTrackEnd(unittest.TestCase):
    # R36.1 @122.53,105.27 on 2026-11-11: armed, the status bar read (122.527, 105.287) over that end.
    OLD = Item("old", "track", "/AMP_EN", "F.Cu", (123.5, 104.3), (122.53, 105.27), 0.25)
    START = PadInfo("R36.1", "/AMP_EN", 119.9, 103.3535, ("F.Cu",), 0.4, False)

    def test_a_loose_end_beside_the_target_is_brought_exactly_onto_it(self):
        new = [Item("a", "track", "/AMP_EN", "F.Cu", (119.9, 103.3535), (121.2, 103.3535), 0.25),
               Item("b", "track", "/AMP_EN", "F.Cu", (121.2, 103.3535), (122.527, 104.6805), 0.25),
               Item("c", "track", "/AMP_EN", "F.Cu", (122.527, 104.6805), (122.527, 105.287), 0.25)]
        moves, deletes, furthest, _ = kr.true_up(new, [self.OLD], [self.START], (122.53, 105.27), 0.125)
        self.assertEqual(deletes, [])
        self.assertEqual(moves["c"][1], (122.53, 105.27))
        self.assertAlmostEqual(moves["c"][0][0], 122.53)
        self.assertLessEqual(furthest, 0.05)
        from kicad_route_quality import validate_polyline
        self.assertEqual(validate_polyline([moves["a"][0], moves["a"][1], moves["b"][1], moves["c"][1]]), [])

    def test_an_end_further_than_a_true_up_may_move_is_left_alone(self):
        new = [Item("a", "track", "/AMP_EN", "F.Cu", (119.9, 103.3535), (122.45, 103.3535), 0.25),
               Item("c", "track", "/AMP_EN", "F.Cu", (122.45, 103.3535), (122.45, 105.2), 0.25)]
        self.assertEqual(kr.true_up(new, [self.OLD], [self.START], (122.53, 105.27), 0.125)[:2], ({}, []))

    def test_the_final_hover_on_a_track_end_takes_half_its_width(self):
        target = kr.copper_end([self.OLD], (122.53, 105.27), self.START)
        self.assertTrue(target.track_end)
        self.assertEqual(target.half, 0.125)
        self.assertFalse(kr.copper_end([Item("v", "via", "/AMP_EN", None, (122.53, 105.27), (122.53, 105.27), 0.6)],
                                       (122.53, 105.27), self.START).track_end)


class BoardLayers(unittest.TestCase):
    def test_a_through_hole_pad_has_only_the_boards_copper_layers(self):
        # U8.35 on 2026-11-11 listed F.Cu, B.Cu and In1.Cu to In30.Cu on this four-layer board.
        board = kr.KipyBoard.__new__(kr.KipyBoard)
        board.is_copper = lambda layer: layer < 100
        board.board = type("B", (), {"get_enabled_layers": lambda self: [0, 2, 4, 6]})()
        self.assertEqual([layer for layer in range(0, 70, 2) if board.copper_here(layer)], [0, 2, 4, 6])
        self.assertFalse(board.copper_here(8))


class Arguments(unittest.TestCase):
    def test_waypoints_keep_their_order(self):
        args = kr.parse_args(["A.1", "B.1", "--through", "1,2", "--via", "3,4", "--through", "5,6"])
        self.assertEqual(args.waypoints, [("through", 1.0, 2.0), ("via", 3.0, 4.0), ("through", 5.0, 6.0)])

    def test_width_parses_positive_mm_and_refuses_bad_values(self):
        self.assertEqual(kr.parse_args(["A.1", "B.1", "--width", "0.5"]).width, 0.5)
        args = kr.parse_args(["A.1", "B.1", "--width", "0.5", "--through", "1,2", "--width-at", "3,4,0.3"])
        self.assertEqual(args.waypoints, [("through", 1.0, 2.0), ("width_at", 3.0, 4.0, 0.3)])
        for bad in ("0", "-0.2", "101", "nan", "abc"):
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                kr.parse_args(["A.1", "B.1", "--width", bad])
        for bad in ("1,2", "1,2,0", "1,2,-0.3", "1,2,3,4"):
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                kr.parse_args(["A.1", "B.1", "--width-at", bad])

    def test_ocr_helper_uses_fast_status_and_accurate_panel(self):
        self.assertIn('case "ocr", "ocr-accurate":', kr.HELPER)
        self.assertIn('request.recognitionLevel = (a[1] == "ocr-accurate") ? .accurate : .fast', kr.HELPER)

        editor = kr.MacEditor.__new__(kr.MacEditor)
        editor.height = 1000.0
        calls = []

        def fake_ocr(x, y, w, h, *, accurate=False):
            calls.append(accurate)
            return ["Z 9.97 X 1.0 Y 2.0 grid 0.05 mm"] if not accurate else ["Routing Track: /M1_B"]

        editor._ocr = fake_ocr
        status = editor.read_status()
        panel = editor.read_panel()
        self.assertEqual((status.x, status.y), (1.0, 2.0))
        self.assertEqual(panel.net, "/M1_B")
        self.assertEqual(calls, [False, True])

    def test_kipy_board_connects_with_60s_ipc_timeout(self):
        import types
        from unittest import mock
        seen = []
        fake_board = types.SimpleNamespace(document=types.SimpleNamespace(board_filename="demo.kicad_pcb", project=types.SimpleNamespace(path="/tmp/demo")))

        class FakeKiCad:
            def __init__(self, **kw):
                seen.append(kw)

            def get_board(self):
                return fake_board

        fake_kipy = types.ModuleType("kipy")
        fake_kipy.KiCad = FakeKiCad
        fake_util = types.ModuleType("kipy.util")
        fake_bl = types.ModuleType("kipy.util.board_layer")
        fake_bl.canonical_name = lambda lid: "F.Cu"
        fake_bl.layer_from_canonical_name = lambda name: 0
        fake_bl.is_copper_layer = lambda lid: True
        with mock.patch.dict(sys.modules, {"kipy": fake_kipy, "kipy.util": fake_util, "kipy.util.board_layer": fake_bl}):
            kr.KipyBoard("99999")
        self.assertEqual(seen, [{"socket_path": None, "timeout_ms": 60000}])


if __name__ == "__main__":
    unittest.main()
