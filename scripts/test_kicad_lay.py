#!/usr/bin/env python3
"""Tests for scripts/kicad_lay.py (pure geometry, pre-checks, CLI ordering, and mocked atomic rollback)."""
from __future__ import annotations

import json
import math
import pathlib
import sys
import unittest
from io import StringIO
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import kicad_lay as lay  # noqa: E402
import kicad_route as kr  # noqa: E402
from kicad_route_quality import PadCopper, compass_direction  # noqa: E402


def make_pad(name: str, net: str, center: tuple[float, float], size: tuple[float, float],
             angle_deg: float = 0.0, shape: str = "roundrect", layers=("F.Cu",), round_ratio: float = 0.25) -> kr.PadInfo:
    copper = PadCopper(center, size, angle_deg, shape, round_ratio, label=name)
    return kr.PadInfo(
        name=name, net=net, x=center[0], y=center[1], layers=layers,
        half=min(size) / 2.0, through=len(layers) > 1, size=size, angle=angle_deg,
        round=(shape == "circle"), copper=copper, others=(),
    )


class FakeLayBoard:
    def __init__(self, pads: dict[str, kr.PadInfo], *, layers=("F.Cu", "B.Cu"), drc_errors=(), quality_result=None,
                 connect=True, nets=None, outline_poly=None, existing_zones=None, empty_pour_layers=()):
        self._pads = pads
        self._layers = layers
        self._drc_errors = list(drc_errors)
        self._quality_result = quality_result
        self._connect = connect
        self._nets = set(nets) if nets is not None else {p.net for p in pads.values() if p.net}
        self._outline_poly = [list(pt) for pt in outline_poly] if outline_poly is not None else [
            [0.0, 0.0], [20.0, 0.0], [20.0, 10.0], [0.0, 10.0]
        ]
        self._empty_pour_layers = set(empty_pour_layers)
        self.live_items: list[kr.Item] = []
        self.live_zones: list[dict] = [dict(z) for z in (existing_zones or ())]
        self.committed = []
        self.rolled_back = []
        self.create_called = 0
        self.refill_called = 0
        self._counter = 0

    def file(self) -> str:
        return "/tmp/fake.kicad_pcb"

    def pad(self, spec: str) -> kr.PadInfo:
        if spec not in self._pads:
            raise kr.RouteFailure("bad_request", f"unknown pad {spec}")
        return self._pads[spec]

    def nets(self) -> set[str]:
        return set(self._nets)

    def enabled_layers(self) -> tuple[str, ...]:
        return self._layers

    def board_outline(self) -> dict:
        xs = [p[0] for p in self._outline_poly]
        ys = [p[1] for p in self._outline_poly]
        return {
            "bbox_mm": [min(xs), min(ys), max(xs), max(ys)] if self._outline_poly else None,
            "polygon_mm": [list(p) for p in self._outline_poly],
        }

    def zones(self) -> list[dict]:
        return [dict(z) for z in self.live_zones]

    def filled_zone_polygons(self, net_name: str, layers=None) -> dict:
        wanted = set(layers) if layers else None
        out: dict = {}
        for z in self.live_zones:
            if z.get("type") != "copper" or z.get("net") != net_name:
                continue
            outline = z.get("outline_mm") or []
            holes = z.get("holes_mm") or []
            if len(outline) < 3:
                continue
            for layer in z.get("filled_layers") or []:
                if wanted is not None and layer not in wanted:
                    continue
                out.setdefault(layer, []).append((outline, holes))
        return out

    def netclass_defaults(self, net_name: str) -> dict:
        return {"track_width_mm": 0.25, "via_diameter_mm": 0.6, "via_drill_mm": 0.3, "clearance_mm": 0.2}

    def items(self) -> list[kr.Item]:
        return list(self.live_items)

    def quality(self) -> dict:
        if self.live_items and self._quality_result is not None:
            return self._quality_result
        return {"errors": {}, "warnings": {}}

    def text(self) -> str:
        return f"(kicad_pcb (items {len(self.live_items)}) (zones {len(self.live_zones)}))"

    def run_drc_diff(self, before_text: str, after_text: str) -> dict:
        return {"new_errors": list(self._drc_errors), "unconnected": [4, 0 if self._connect else 2]}

    def create_copper(self, segments, vias, message: str, *, pours=(), replace_zone_ids=(),
                      refill_zones: bool = False, run_drc: bool = True) -> dict:
        self.create_called += 1
        need_refill = bool(pours or replace_zone_ids or refill_zones)
        if need_refill:
            self.refill_called += 1
        before_ids = {i.id for i in self.live_items}
        before_zones = [dict(z) for z in self.live_zones]
        by_zid = {z["id"]: z for z in self.live_zones}
        for zid in replace_zone_ids:
            rz = by_zid.get(zid)
            if rz is None:
                raise lay.LayFailure("not_found", f"zone {zid} not found")
            if rz["type"] == "rule_area":
                raise lay.LayFailure("forbidden", f"zone {zid} is a rule area")
            if rz.get("locked"):
                raise lay.LayFailure("locked", f"zone {zid} is locked")
        if replace_zone_ids:
            rep = set(replace_zone_ids)
            self.live_zones = [z for z in self.live_zones if z["id"] not in rep]

        created_ids = set()
        added = []
        for seg in segments:
            self._counter += 1
            cid = f"t-{self._counter}"
            created_ids.add(cid)
            if self._connect:
                added.append(kr.Item(cid, "track", seg.net, seg.layer, seg.start, seg.end, seg.width))
        for via in vias:
            self._counter += 1
            cid = f"v-{self._counter}"
            created_ids.add(cid)
            added.append(kr.Item(cid, "via", via.net, None, via.at, via.at, via.diameter))
        if not self._connect and segments:
            # Keep via items intact while putting tracks at disconnected coordinates.
            added = [
                kr.Item(cid, "track", segments[0].net, segments[0].layer, (900.0, 900.0), (901.0, 900.0), 0.2)
                for cid in sorted(created_ids) if cid.startswith("t-")
            ] + [item for item in added if item.kind == "via"]
        self.live_items.extend(added)

        created_zone_ids = set()
        for pour in pours:
            self._counter += 1
            zid = f"z-{self._counter}"
            created_zone_ids.add(zid)
            filled_layers = sorted(l for l in pour.layers if l not in self._empty_pour_layers)
            self.live_zones.append({
                "id": zid,
                "name": pour.name,
                "type": "copper",
                "net": pour.net,
                "layers": list(pour.layers),
                "priority": pour.priority,
                "clearance_mm": pour.clearance,
                "min_thickness_mm": pour.min_thickness,
                "connect": pour.connect,
                "thermal_gap_mm": pour.thermal_gap,
                "thermal_bridge_width_mm": pour.thermal_bridge_width,
                "island_mode": pour.island_mode,
                "outline_mm": [list(p) for p in pour.outline],
                "holes_mm": [[list(p) for p in h] for h in pour.holes],
                "filled": bool(filled_layers),
                "filled_layers": filled_layers,
                "locked": False,
            })
        return {
            "before_ids": before_ids,
            "created_ids": created_ids,
            "created_zone_ids": created_zone_ids,
            "before_zones": before_zones,
            "has_zones": bool(pours or replace_zone_ids),
            "need_refill": need_refill,
            "before_quality": {"errors": {}, "warnings": {}},
            "before_text": "(kicad_pcb (items 0))",
            "commit": object(),
            "pushed": False,
            "added": added,
        }

    def delete_zones(self, zone_ids: list[str], message: str, *, run_drc: bool = True) -> dict:
        by_id = {z["id"]: z for z in self.live_zones}
        for zid in zone_ids:
            z = by_id.get(zid)
            if z is None:
                raise lay.LayFailure("not_found", f"zone {zid} not found on board")
            if z["type"] == "rule_area":
                raise lay.LayFailure("forbidden", f"zone {zid} is a rule area")
            if z.get("locked"):
                raise lay.LayFailure("locked", f"zone {zid} is locked")
        before_zones = [dict(z) for z in self.live_zones]
        before_text = self.text()
        del_set = set(zone_ids)
        self.live_zones = [z for z in self.live_zones if z["id"] not in del_set]
        drc = {"skipped": True}
        if run_drc:
            drc = self.run_drc_diff(before_text, self.text())
            if drc["new_errors"]:
                self.live_zones = before_zones
                raise lay.LayFailure("drc_failed", f"new DRC error: {drc['new_errors'][0].get('description')}",
                                     violations=drc["new_errors"], drc=drc)
        return {"deleted_ids": list(zone_ids), "drc": drc}

    def finalize_commit(self, tx: dict, message: str) -> None:
        tx["pushed"] = True
        self.committed.append(set(tx["created_ids"]) | set(tx.get("created_zone_ids", set())))

    def rollback_commit(self, tx: dict, message: str) -> None:
        if tx.get("need_refill"):
            self.refill_called += 1
        self.live_items = [item for item in self.live_items if item.id not in tx["created_ids"]]
        if "before_zones" in tx:
            self.live_zones = list(tx["before_zones"])
        self.rolled_back.append(set(tx["created_ids"]) | set(tx.get("created_zone_ids", set())))


class GeometryTests(unittest.TestCase):
    def test_facing_escape_handles_all_orientations(self):
        cases = [
            (0.0, (0.5, 1.5), (0.0, 5.0), (0.0, 1.0), (0.0, 0.75), (0.0, 1.0)),
            (180.0, (0.5, 1.5), (0.0, -5.0), (0.0, -1.0), (0.0, -0.75), (0.0, -1.0)),
            (90.0, (0.5, 1.5), (5.0, 0.0), (1.0, 0.0), (0.75, 0.0), (1.0, 0.0)),
            (270.0, (0.5, 1.5), (-5.0, 0.0), (-1.0, 0.0), (-0.75, 0.0), (-1.0, 0.0)),
            (0.0, (1.5, 0.5), (-5.0, 0.0), (-1.0, 0.0), (-0.75, 0.0), (-1.0, 0.0)),
            (45.0, (0.5, 1.5), (-4.0, -4.0), (-math.sqrt(0.5), -math.sqrt(0.5)), None, None),
        ]
        for angle, size, other, exp_u, exp_edge, exp_esc in cases:
            with self.subTest(angle=angle, size=size, other=other):
                pad = make_pad("J1.1", "/A", (0.0, 0.0), size, angle_deg=angle)
                esc = lay.facing_escape(pad, other, escape_mm=0.25)
                self.assertAlmostEqual(esc["unit"][0], exp_u[0], places=4)
                self.assertAlmostEqual(esc["unit"][1], exp_u[1], places=4)
                if exp_edge:
                    self.assertEqual(esc["edge"], exp_edge)
                    self.assertEqual(esc["escape"], exp_esc)
        # Close facing pads clamp escape distance cleanly instead of overshooting.
        close_pad = make_pad("J1.2", "/A", (0.0, 0.0), (0.5, 1.5), angle_deg=0.0)
        esc = lay.facing_escape(close_pad, (0.0, 1.8), escape_mm=1.0)
        self.assertLess(esc["escape"][1], 1.8)
        self.assertGreater(esc["escape"][1], 0.75)

    def test_expand_45_leg_and_merge_collinear_avoid_90_deg_corners(self):
        # Straight vertical pair collapses into a single segment with centered_exits.
        pads = {
            "J1.1": make_pad("J1.1", "/M1", (0.0, 0.0), (0.5, 1.5)),
            "J2.1": make_pad("J2.1", "/M1", (0.0, 5.0), (0.5, 1.5)),
        }
        board = FakeLayBoard(pads)
        plan = lay.plan_route({"from": "J1.1", "to": "J2.1", "centered_exits": True}, board)
        self.assertEqual(len(plan["segments"]), 1)
        self.assertEqual((plan["segments"][0].start, plan["segments"][0].end), ((0.0, 0.0), (0.0, 5.0)))

        # Diagonal/offset pair chooses obtuse 135-deg corners cleanly.
        pads["J2.1"] = make_pad("J2.1", "/M1", (2.0, 6.0), (0.5, 1.5))
        plan_off = lay.plan_route({"from": "J1.1", "to": "J2.1", "centered_exits": True}, FakeLayBoard(pads))
        dirs = [compass_direction(s.start, s.end) for s in plan_off["segments"]]
        self.assertNotIn(None, dirs)
        for d1, d2 in zip(dirs, dirs[1:]):
            self.assertGreater(d1[0] * d2[0] + d1[1] * d2[1], 0)

    def test_centered_exit_precheck_rejects_bent_exit_before_touching_board(self):
        pads = {
            "J1.1": make_pad("J1.1", "/M1", (0.0, 0.0), (0.5, 1.5)),
            "J2.1": make_pad("J2.1", "/M1", (0.0, 5.0), (0.5, 1.5)),
        }
        board = FakeLayBoard(pads)
        # Waypoint inside J1.1 off-axis violates straight pad exit -> caught by verify_centered_exits.
        spec = {"routes": [{"from": "J1.1", "to": "J2.1", "centered_exits": True, "steps": [[0.4, 0.2]]}]}
        with self.assertRaises(lay.LayFailure) as ctx:
            lay.execute_batch(board, spec, run_drc=False)
        self.assertEqual(ctx.exception.status, "pad_exit_failed")
        self.assertEqual(board.create_called, 0)

    def test_centered_exit_precheck_rejects_via_touching_pad_and_allows_clear_via(self):
        pads = {
            "J1.1": make_pad("J1.1", "/M1_A", (117.25, 106.61), (0.6, 1.0), angle_deg=180.0),
            "J2.1": make_pad("J2.1", "/M1_A", (105.75, 116.60), (0.3, 1.0), angle_deg=0.0),
        }
        bad_board = FakeLayBoard(pads)
        bad_route = {
            "from": "J1.1", "to": "J2.1", "layer": "F.Cu", "width": 0.5,
            "via_diameter": 0.6, "via_drill": 0.3, "centered_exits": True,
            "steps": [{"via": [117.25, 107.40], "layer": "B.Cu"}, [105.75, 107.80], {"via": [105.75, 115.00], "layer": "F.Cu"}],
        }
        with self.assertRaises(lay.LayFailure) as ctx:
            lay.plan_route(bad_route, bad_board)
        self.assertEqual(ctx.exception.status, "pad_exit_failed")
        self.assertIn("touches pad edge", ctx.exception.reason)

        good_board = FakeLayBoard(pads)
        good_route = {
            "from": "J1.1", "to": "J2.1", "layer": "F.Cu", "width": 0.5,
            "via_diameter": 0.6, "via_drill": 0.3, "centered_exits": True,
            "steps": [{"via": [117.25, 107.45], "layer": "B.Cu"}, [105.75, 107.85], {"via": [105.75, 115.00], "layer": "F.Cu"}],
        }
        plan = lay.plan_route(good_route, good_board)
        self.assertEqual(len(plan["vias"]), 2)


class ExecuteAndRollbackTests(unittest.TestCase):
    def setUp(self):
        self.pads = {
            "J1.1": make_pad("J1.1", "/S1", (0.0, 0.0), (0.5, 1.5)),
            "J2.1": make_pad("J2.1", "/S1", (0.0, 5.0), (0.5, 1.5)),
            "J1.2": make_pad("J1.2", "/S2", (1.0, 0.0), (0.5, 1.5), layers=("F.Cu",)),
            "J2.2": make_pad("J2.2", "/S2", (1.0, 5.0), (0.5, 1.5), layers=("B.Cu",)),
        }

    def test_pads_route_ordered_steps_and_batch_commit(self):
        board = FakeLayBoard(self.pads)
        out = StringIO()
        with mock.patch("sys.stdout", out):
            rc = lay.main(["pads", "J1.1", "--facing", "J2.1"], board=board)
        self.assertEqual(rc, 0)
        info = json.loads(out.getvalue())
        self.assertEqual(info["pads"][0]["facing"]["escape_mm"], [0.0, 1.0])

        # CLI preserves alternating --through / --via order and switches layers at --via.
        out2 = StringIO()
        with mock.patch("sys.stdout", out2):
            rc = lay.main([
                "route", "J1.2", "J2.2", "--layer", "F.Cu", "--through", "1.0,1.5", "--via", "2.0,2.5",
                "--through", "2.0,3.5", "--centered-exits",
            ], board=board)
        self.assertEqual(rc, 0, out2.getvalue())
        res = json.loads(out2.getvalue())
        self.assertEqual((res["status"], res["routes"][0]["vias"]), ("laid", 1))
        self.assertEqual(len(board.committed), 1)
        self.assertEqual(board.rolled_back, [])

        # Multi-route atomic batch.
        board2 = FakeLayBoard(self.pads)
        spec = {"centered_exits": True, "routes": [
            {"from": "J1.1", "to": "J2.1", "layer": "F.Cu"},
            {"from": "J1.2", "to": "J2.2", "layer": "F.Cu", "steps": [{"via": [1.0, 2.5], "layer": "B.Cu"}]},
        ]}
        out3 = StringIO()
        with mock.patch("sys.stdout", out3):
            rc = lay.main(["batch", "--json", json.dumps(spec)], board=board2)
        self.assertEqual(rc, 0, out3.getvalue())
        batch_res = json.loads(out3.getvalue())
        self.assertEqual(len(batch_res["routes"]), 2)
        self.assertEqual(batch_res["vias_created"], 1)

    def test_rollback_on_route_quality_error_drc_error_and_open_connection(self):
        bad_q = {"errors": {("error", "sharp_corner", "/S1", 0, (0, 2), "90 deg"): "error: sharp_corner"}, "warnings": {}}
        for kwargs, expected_status in (
            ({"quality_result": bad_q}, "route_quality"),
            ({"drc_errors": [{"type": "clearance", "description": "Track near pad"}]}, "drc_failed"),
            ({"connect": False}, "not_connected"),
        ):
            with self.subTest(expected_status=expected_status):
                board = FakeLayBoard(self.pads, **kwargs)
                out = StringIO()
                with mock.patch("sys.stdout", out):
                    rc = lay.main(["route", "J1.1", "J2.1", "--layer", "F.Cu"], board=board)
                self.assertEqual(rc, 1)
                res = json.loads(out.getvalue())
                self.assertFalse(res["ok"])
                self.assertEqual(res["status"], expected_status)
                self.assertEqual(board.committed, [])
                self.assertEqual(len(board.rolled_back), 1)
                self.assertEqual(board.live_items, [])

    def test_warning_only_route_quality_is_preserved_and_reported(self):
        warn_q = {"errors": {}, "warnings": {("warning", "micro_segment", "/S1", 0, (0, 1), "0.14 mm"): "warning: micro_segment"}}
        board = FakeLayBoard(self.pads, quality_result=warn_q)
        out = StringIO()
        with mock.patch("sys.stdout", out):
            rc = lay.main(["route", "J1.1", "J2.1", "--layer", "F.Cu", "--centered-exits"], board=board)
        self.assertEqual(rc, 0)
        res = json.loads(out.getvalue())
        self.assertEqual(res["status"], "laid")
        self.assertEqual(res["quality_warnings"], ["warning: micro_segment"])
        self.assertEqual(len(board.committed), 1)


class PourAndZoneTests(unittest.TestCase):
    def _board(self, **kwargs):
        pads = {
            "J1.1": make_pad("J1.1", "GND", (2.0, 2.0), (0.6, 1.0), layers=("F.Cu", "B.Cu")),
            "J2.1": make_pad("J2.1", "GND", (12.0, 2.0), (0.6, 1.0), layers=("F.Cu", "B.Cu")),
            "J1.2": make_pad("J1.2", "+5V", (2.0, 8.0), (0.6, 1.0), layers=("F.Cu", "B.Cu")),
        }
        return FakeLayBoard(pads, layers=kwargs.pop("layers", ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")), **kwargs)

    def test_execute_zones_inspects_layers_outline_and_existing_zones(self):
        board = self._board(
            existing_zones=[
                {"id": "ka-1", "name": "antenna_keepout", "type": "rule_area", "net": "", "layers": ["F.Cu", "B.Cu"],
                 "priority": 0, "clearance_mm": None, "min_thickness_mm": None, "connect": None,
                 "thermal_gap_mm": None, "thermal_bridge_width_mm": None, "island_mode": None,
                 "outline_mm": [[1, 1], [4, 1], [4, 4], [1, 4]], "holes_mm": [], "filled": False, "filled_layers": [], "locked": False}
            ]
        )
        res = lay.execute_zones(board)
        self.assertTrue(res["ok"])
        self.assertEqual(res["verb"], "zones")
        self.assertEqual(res["enabled_layers"], ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])
        self.assertEqual(res["board_outline"]["bbox_mm"], [0.0, 0.0, 20.0, 10.0])
        self.assertEqual(len(res["zones"]), 1)
        self.assertEqual(res["zones"][0]["type"], "rule_area")

    def test_pour_board_outline_rect_and_l_polygon_inset(self):
        board = self._board()
        res = lay.execute_batch(board, {
            "pours": [{
                "net": "GND",
                "layer": "In1.Cu",
                "board_outline": True,
                "inset": 0.25,
                "connect": "thermal",
                "thermal_gap": 0.25,
                "thermal_bridge_width": 0.25,
            }]
        })
        self.assertEqual(res["verb"], "pour")
        self.assertEqual(res["zones_created"], 1)
        self.assertEqual(res["pours"][0]["filled_layers"], ["In1.Cu"])
        zone = board.live_zones[0]
        self.assertEqual(zone["outline_mm"], [[0.25, 0.25], [19.75, 0.25], [19.75, 9.75], [0.25, 9.75]])

        # Non-rectangular L-shaped board outline inset
        l_outline = [[0.0, 0.0], [20.0, 0.0], [20.0, 5.0], [10.0, 5.0], [10.0, 15.0], [0.0, 15.0]]
        l_board = self._board(outline_poly=l_outline)
        lay.execute_batch(l_board, {"pours": [{"net": "GND", "layers": ["In1.Cu", "In2.Cu"], "outline": "board", "inset": 0.5}]})
        self.assertEqual(
            l_board.live_zones[0]["outline_mm"],
            [[0.5, 0.5], [19.5, 0.5], [19.5, 4.5], [9.5, 4.5], [9.5, 14.5], [0.5, 14.5]],
        )
        self.assertEqual(l_board.live_zones[0]["layers"], ["In1.Cu", "In2.Cu"])

    def test_thermal_pours_default_non_zero_spokes_while_solid_and_none_stay_unset(self):
        board = self._board()
        res_default = lay.execute_batch(board, {
            "pours": [
                {"net": "GND", "layer": "In1.Cu", "board_outline": True, "inset": 0.5},
                {"net": "+5V", "layer": "In2.Cu", "board_outline": True, "inset": 0.5, "connect": "pth_thermal"},
            ]
        })
        self.assertEqual(len(res_default["pours"]), 2)
        self.assertEqual(board.live_zones[0]["thermal_gap_mm"], 0.5)
        self.assertEqual(board.live_zones[0]["thermal_bridge_width_mm"], 0.5)
        self.assertEqual(board.live_zones[1]["thermal_gap_mm"], 0.5)
        self.assertEqual(board.live_zones[1]["thermal_bridge_width_mm"], 0.5)

        board2 = self._board()
        lay.execute_batch(board2, {
            "pours": [
                {"net": "GND", "layer": "In1.Cu", "board_outline": True, "inset": 0.5,
                 "thermal_gap": 0.35, "thermal_bridge": 0.4},
                {"net": "+5V", "layer": "In2.Cu", "board_outline": True, "inset": 0.5, "connect": "solid"},
                {"net": "GND", "layer": "F.Cu", "board_outline": True, "inset": 0.5, "connect": "none"},
            ]
        })
        self.assertEqual(board2.live_zones[0]["thermal_gap_mm"], 0.35)
        self.assertEqual(board2.live_zones[0]["thermal_bridge_width_mm"], 0.4)
        self.assertIsNone(board2.live_zones[1]["thermal_gap_mm"])
        self.assertIsNone(board2.live_zones[1]["thermal_bridge_width_mm"])
        self.assertIsNone(board2.live_zones[2]["thermal_gap_mm"])
        self.assertIsNone(board2.live_zones[2]["thermal_bridge_width_mm"])

    def test_extract_outline_polygon_stitches_arcs_and_segments(self):
        class Vec:
            def __init__(self, x, y):
                self.x, self.y = lay.to_nm(x), lay.to_nm(y)

        class Seg:
            layer = "Edge.Cuts"

            def __init__(self, a, b):
                self.start, self.end = Vec(*a), Vec(*b)

        class Arc:
            layer = "Edge.Cuts"

            def __init__(self, a, m, b):
                self.start, self.mid, self.end = Vec(*a), Vec(*m), Vec(*b)

        shapes = [
            Seg((0.0, 0.0), (10.0, 0.0)),
            Arc((10.0, 0.0), (10.0 + math.sqrt(0.5) * 2.0, 2.0 - math.sqrt(0.5) * 2.0), (12.0, 2.0)),
            Seg((12.0, 2.0), (12.0, 10.0)),
            Seg((12.0, 10.0), (0.0, 10.0)),
            Seg((0.0, 10.0), (0.0, 0.0)),
        ]
        outline = lay.extract_outline_polygon(shapes)
        self.assertEqual(outline["bbox_mm"], [0.0, 0.0, 12.0, 10.0])
        self.assertGreater(len(outline["polygon_mm"]), 5)

    def test_rect_and_polygon_with_hole_replace_and_refusals(self):
        board = self._board(
            existing_zones=[
                {"id": "ka-1", "name": "ko", "type": "rule_area", "net": "", "layers": ["In1.Cu"],
                 "priority": 0, "clearance_mm": None, "min_thickness_mm": None, "connect": None,
                 "thermal_gap_mm": None, "thermal_bridge_width_mm": None, "island_mode": None,
                 "outline_mm": [[0, 0], [5, 0], [5, 5], [0, 5]], "holes_mm": [], "filled": False, "filled_layers": [], "locked": False},
                {"id": "old-gnd", "name": "", "type": "copper", "net": "GND", "layers": ["In1.Cu"],
                 "priority": 0, "clearance_mm": 0.2, "min_thickness_mm": 0.2, "connect": "thermal",
                 "thermal_gap_mm": None, "thermal_bridge_width_mm": None, "island_mode": "always",
                 "outline_mm": [[0, 0], [10, 0], [10, 10], [0, 10]], "holes_mm": [], "filled": True, "filled_layers": ["In1.Cu"], "locked": False},
                {"id": "lock-1", "name": "", "type": "copper", "net": "+5V", "layers": ["In2.Cu"],
                 "priority": 0, "clearance_mm": 0.2, "min_thickness_mm": 0.2, "connect": "solid",
                 "thermal_gap_mm": None, "thermal_bridge_width_mm": None, "island_mode": "always",
                 "outline_mm": [[0, 0], [10, 0], [10, 10], [0, 10]], "holes_mm": [], "filled": True, "filled_layers": ["In2.Cu"], "locked": True},
            ]
        )
        res = lay.execute_batch(board, {
            "pours": [{
                "net": "GND",
                "layer": "In1.Cu",
                "polygon": [[1, 1], [19, 1], [19, 9], [1, 9]],
                "holes": [[[4, 4], [6, 4], [6, 6], [4, 6]]],
                "replace": True,
                "connect": "solid",
                "priority": 2,
                "island_mode": "area",
                "min_island_area": 5.0,
            }]
        })
        self.assertEqual(res["zones_replaced"], 1)
        ids_left = {z["id"] for z in board.live_zones}
        self.assertIn("ka-1", ids_left)
        self.assertNotIn("old-gnd", ids_left)

        # Explicitly replacing a rule area or deleting locked/rule_area must fail closed.
        with self.assertRaises(lay.LayFailure) as ctx_ka:
            lay.execute_batch(board, {"pours": [{"net": "GND", "layer": "In1.Cu", "rect": [1, 1, 8, 8], "replace_ids": ["ka-1"]}]})
        self.assertEqual(ctx_ka.exception.status, "forbidden")

        with self.assertRaises(lay.LayFailure) as ctx_del_ka:
            lay.execute_delete_zones(board, ["ka-1"])
        self.assertEqual(ctx_del_ka.exception.status, "forbidden")

        with self.assertRaises(lay.LayFailure) as ctx_del_lock:
            lay.execute_delete_zones(board, ["lock-1"])
        self.assertEqual(ctx_del_lock.exception.status, "locked")

        with self.assertRaises(lay.LayFailure) as ctx_missing:
            lay.execute_delete_zones(board, ["does-not-exist"])
        self.assertEqual(ctx_missing.exception.status, "not_found")

        # Delete the created copper zone cleanly
        new_zid = res["pours"][0]["zone_ids"][0]
        del_res = lay.execute_delete_zones(board, [new_zid])
        self.assertEqual(del_res["status"], "deleted")
        self.assertEqual(del_res["deleted_ids"], [new_zid])
        self.assertNotIn(new_zid, {z["id"] for z in board.live_zones})

    def test_batch_combines_routes_pours_and_stitching_vias_and_rolls_back(self):
        board = self._board()
        res = lay.execute_batch(board, {
            "routes": [{"from": "J1.1", "to": "J2.1", "net": "GND", "layer": "F.Cu"}],
            "pours": [{"net": "GND", "layer": "In1.Cu", "board_outline": True, "inset": 0.25}],
            "vias": [{"at": [6.0, 5.0], "net": "GND"}],
        })
        self.assertEqual(res["verb"], "batch")
        self.assertEqual(res["zones_created"], 1)
        self.assertEqual(res["vias_created"], 1)
        self.assertEqual(len(board.live_zones), 1)

        # Rollback on empty pour
        empty_board = self._board(empty_pour_layers=("In1.Cu",))
        with self.assertRaises(lay.LayFailure) as ctx_empty:
            lay.execute_batch(empty_board, {
                "pours": [{"net": "GND", "layer": "In1.Cu", "rect": [1, 1, 10, 8]}],
                "vias": [{"at": [5.0, 5.0], "net": "GND"}],
            })
        self.assertEqual(ctx_empty.exception.status, "empty_pour")
        self.assertEqual(empty_board.live_items, [])
        self.assertEqual(empty_board.live_zones, [])

        # Rollback on DRC failure also rolls back created zones and vias
        drc_board = self._board(drc_errors=[{"description": "shorted_items"}])
        with self.assertRaises(lay.LayFailure) as ctx_drc:
            lay.execute_batch(drc_board, {
                "pours": [{"net": "+5V", "layer": "In2.Cu", "rect": [1, 1, 10, 8]}],
                "vias": [{"at": [5.0, 5.0], "net": "GND"}],
            })
        self.assertEqual(ctx_drc.exception.status, "drc_failed")
        self.assertEqual(drc_board.live_items, [])
        self.assertEqual(drc_board.live_zones, [])

        # Rollback if KiCad silently reassigns a standalone via's net on read-back
        re_net_board = self._board()
        orig_create = re_net_board.create_copper

        def steal_via_net(segments, vias, message, **kw):
            tx = orig_create(segments, vias, message, **kw)
            for idx, item in enumerate(re_net_board.live_items):
                if item.kind == "via" and item.id in tx["created_ids"]:
                    re_net_board.live_items[idx] = kr.Item(item.id, "via", "+5V", item.layer, item.start, item.end, item.width)
            return tx

        re_net_board.create_copper = steal_via_net
        with self.assertRaises(lay.LayFailure) as ctx_net:
            lay.execute_batch(re_net_board, {
                "pours": [{"net": "GND", "layer": "In1.Cu", "rect": [1, 1, 10, 8]}],
                "vias": [{"at": [5.0, 5.0], "net": "GND"}],
            })
        self.assertEqual(ctx_net.exception.status, "read_back_differs")
        self.assertEqual(re_net_board.live_items, [])
        self.assertEqual(re_net_board.live_zones, [])

    def test_delete_zones_rollback_on_drc_failure(self):
        board = self._board(
            drc_errors=[{"description": "unconnected"}],
            existing_zones=[
                {"id": "z-1", "name": "", "type": "copper", "net": "GND", "layers": ["In1.Cu"],
                 "priority": 0, "clearance_mm": 0.2, "min_thickness_mm": 0.2, "connect": "thermal",
                 "thermal_gap_mm": None, "thermal_bridge_width_mm": None, "island_mode": "always",
                 "outline_mm": [[0, 0], [10, 0], [10, 10], [0, 10]], "holes_mm": [], "filled": True, "filled_layers": ["In1.Cu"], "locked": False}
            ],
        )
        with self.assertRaises(lay.LayFailure) as ctx:
            lay.execute_delete_zones(board, ["z-1"])
        self.assertEqual(ctx.exception.status, "drc_failed")
        self.assertEqual([z["id"] for z in board.live_zones], ["z-1"])

    def test_pour_and_via_input_validation(self):
        board = self._board()
        bad_specs = [
            {"net": "NO_SUCH_NET", "layer": "In1.Cu", "rect": [1, 1, 5, 5]},
            {"net": "GND", "layer": "In9.Cu", "rect": [1, 1, 5, 5]},
            {"net": "GND", "layer": "In1.Cu", "polygon": [[0, 0], [5, 5], [5, 0], [0, 5]]},  # self-intersecting bowtie
            {"net": "GND", "layer": "In1.Cu", "rect": [0, 0, 1, 1], "inset": 0.6},  # collapsing inset
            {"net": "GND", "layer": "In1.Cu", "rect": [1, 1, 5, 5], "connect": "bogus"},
            {"net": "GND", "layer": "In1.Cu", "rect": [1, 1, 5, 5], "island_mode": "bogus"},
            {"net": "GND", "layer": "In1.Cu", "rect": [1, 1, 5, 5], "priority": -1},
        ]
        for spec in bad_specs:
            with self.subTest(spec=spec):
                with self.assertRaises(lay.LayFailure):
                    lay.plan_pour(spec, board)

        with self.assertRaises(lay.LayFailure):
            lay.plan_via({"at": [1, 1], "net": "NO_SUCH"}, board)

    def test_point_in_ring_and_filled_zone_polygons(self):
        outer = [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]]
        hole = [[3.0, 3.0], [6.0, 3.0], [6.0, 6.0], [3.0, 6.0]]
        self.assertTrue(lay.point_in_ring((5.0, 5.0), outer))
        self.assertTrue(lay.point_in_ring((0.0, 5.0), outer))  # on boundary
        self.assertFalse(lay.point_in_ring((-0.1, 5.0), outer))
        self.assertTrue(lay.point_in_filled_zone((2.0, 2.0), [(outer, [hole])]))
        self.assertFalse(lay.point_in_filled_zone((4.5, 4.5), [(outer, [hole])]))
        self.assertFalse(lay.point_in_filled_zone((12.0, 5.0), [(outer, [hole])]))

    def test_to_via_stub_connects_into_filled_pour_and_rejects_outside_or_net_flip(self):
        gnd_zone = {
            "id": "z-gnd", "name": "GND", "type": "copper", "net": "GND", "layers": ["In1.Cu"],
            "priority": 0, "clearance_mm": 0.2, "min_thickness_mm": 0.2, "connect": "thermal",
            "thermal_gap_mm": None, "thermal_bridge_width_mm": None, "island_mode": "always",
            "outline_mm": [[0.5, 0.5], [19.5, 0.5], [19.5, 9.5], [0.5, 9.5]],
            "holes_mm": [[[9.0, 2.0], [11.0, 2.0], [11.0, 6.0], [9.0, 6.0]]],
            "filled": True, "filled_layers": ["In1.Cu"], "locked": False,
        }
        board = self._board(existing_zones=[gnd_zone])
        res = lay.execute_batch(board, {
            "width": 0.4,
            "via_diameter": 0.6,
            "via_drill": 0.3,
            "centered_exits": True,
            "routes": [
                {"from": "J1.1", "to_via": [2.0, 4.0], "pour_layer": "In1.Cu", "net": "GND", "layer": "F.Cu"},
            ],
        })
        self.assertEqual(res["status"], "laid")
        self.assertEqual(res["vias_created"], 1)
        self.assertEqual(res["routes"][0]["to_via"], [2.0, 4.0])
        self.assertEqual(res["routes"][0]["pour_layers"], ["In1.Cu"])
        self.assertEqual(board.refill_called, 1)

        # Via landing inside a zone hole cutout fails with not_in_pour and rolls back + refills.
        bad_hole_board = self._board(existing_zones=[gnd_zone])
        with self.assertRaises(lay.LayFailure) as ctx_hole:
            lay.execute_batch(bad_hole_board, {
                "routes": [{"from": "J2.1", "to_via": [10.0, 4.0], "pour_layer": "In1.Cu", "net": "GND", "layer": "F.Cu"}],
            })
        self.assertEqual(ctx_hole.exception.status, "not_in_pour")
        self.assertEqual(bad_hole_board.live_items, [])
        self.assertEqual(bad_hole_board.refill_called, 2)

        # KiCad silently reassigning to_via net on read-back fails with read_back_differs and rolls back.
        flip_board = self._board(existing_zones=[gnd_zone])
        orig_create = flip_board.create_copper

        def steal_via_net(segments, vias, message, **kw):
            tx = orig_create(segments, vias, message, **kw)
            for idx, item in enumerate(flip_board.live_items):
                if item.kind == "via" and item.id in tx["created_ids"]:
                    flip_board.live_items[idx] = kr.Item(item.id, "via", "+5V", item.layer, item.start, item.end, item.width)
            return tx

        flip_board.create_copper = steal_via_net
        with self.assertRaises(lay.LayFailure) as ctx_flip:
            lay.execute_batch(flip_board, {
                "routes": [{"from": "J1.1", "to_via": [2.0, 4.0], "net": "GND", "layer": "F.Cu"}],
            })
        self.assertEqual(ctx_flip.exception.status, "read_back_differs")
        self.assertEqual(flip_board.live_items, [])

    def test_fail_fast_schema_validation_runs_before_ipc_board_connection(self):
        bad_specs = [
            json.dumps({"routes": [{"from": "J1.1", "to": [10.0, 20.0], "net": "GND"}]}),
            json.dumps({"routes": [{"from": "J1.1", "to": "J2.1", "to_via": [10.0, 20.0]}]}),
            json.dumps({"routes": [{"from": "J1", "to": "J2.1"}]}),
            json.dumps({"routes": []}),
        ]
        for idx, raw_spec in enumerate(bad_specs):
            with self.subTest(idx=idx, raw_spec=raw_spec):
                out = StringIO()
                with mock.patch("sys.stdout", out), \
                        mock.patch.object(lay.mac, "LOCAL", False), \
                        mock.patch.object(lay, "KipyLayBoard", side_effect=AssertionError("IPC should not be called")):
                    rc = lay.main(["batch", "--json", raw_spec])
                self.assertEqual(rc, 1)
                payload = json.loads(out.getvalue())
                self.assertFalse(payload["ok"])
                self.assertEqual(payload["status"], "bad_request")
                if idx == 0:
                    self.assertIn("to_via", payload["reason"])


class _V27Board:
    def __init__(self, pads, items=None):
        self._pads = {p.name: p for p in pads}
        self._items = list(items or [])
        self.create_calls = 0

    def enabled_layers(self):
        return ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")

    def pad(self, ref):
        if ref not in self._pads:
            raise lay.LayFailure("bad_request", f"unknown pad {ref}")
        return self._pads[ref]

    def netclass_defaults(self, _net):
        return {"track_width_mm": 0.25, "via_diameter_mm": 0.6, "via_drill_mm": 0.3}

    def items(self):
        return list(self._items)

    def zones(self):
        return []

    def quality(self):
        return {"errors": {}, "warnings": {}}

    def text(self):
        return ""

    def run_drc_diff(self, _before, _after):
        return {"new_errors": [], "unconnected": [0, 0]}

    def create_copper(self, segments, vias, _label, run_drc=True, **_kwargs):
        self.create_calls += 1
        snap = list(self._items)
        created = set()
        for idx, seg in enumerate(segments):
            item_id = f"seg-{self.create_calls}-{idx}"
            created.add(item_id)
            self._items.append(kr.Item(item_id, "track", seg.net, seg.layer, seg.start, seg.end, seg.width))
        for idx, via in enumerate(vias):
            item_id = f"via-{self.create_calls}-{idx}"
            created.add(item_id)
            self._items.append(kr.Item(item_id, "via", via.net, None, via.at, via.at, via.diameter))
        return {
            "created_ids": created,
            "before_quality": {"errors": {}, "warnings": {}},
            "before_text": "",
            "snap": snap,
        }

    def finalize_commit(self, _tx, _label):
        return None

    def rollback_commit(self, tx, _label):
        self._items = list(tx.get("snap", []))


class V27LayTests(unittest.TestCase):
    @staticmethod
    def _hpad(name, net, x, y, size=(1.6, 0.4), layer="F.Cu"):
        from types import SimpleNamespace

        return kr.PadInfo(
            name=name,
            net=net,
            x=x,
            y=y,
            layers=(layer,),
            half=min(size) / 2.0,
            through=False,
            size=size,
            copper=SimpleNamespace(center=(x, y), size=size, shape="rect", angle_deg=0.0),
        )

    def test_same_column_centered_exits_with_steps_succeeds_and_empty_steps_fails_actionably(self) -> None:
        pads = [
            self._hpad("U2.6", "/F_CLK", 143.137, 100.475),
            self._hpad("U2.8", "/F_CLK", 143.137, 101.125),
        ]
        board = _V27Board(pads)
        with self.assertRaises(lay.LayFailure) as ctx:
            lay.plan_route({"from": "U2.6", "to": "U2.8", "centered_exits": True}, board)
        self.assertEqual(ctx.exception.status, "bad_request")
        self.assertIn("steps", ctx.exception.reason)
        self.assertIn("centered_exits", ctx.exception.reason)

        res = lay.execute_batch(
            board,
            {
                "centered_exits": True,
                "routes": [{"from": "U2.6", "to": "U2.8", "layer": "F.Cu", "steps": [[141.5, 100.8]]}],
            },
        )
        self.assertEqual(res["status"], "laid")
        self.assertEqual(board.create_calls, 1)

    def test_from_and_to_point_list_fail_fast_cli_without_kicad(self) -> None:
        import io
        import json
        from unittest import mock

        out1 = io.StringIO()
        with mock.patch("sys.stdout", out1):
            code1 = lay.main([
                "batch", "--json", json.dumps({"routes": [{"from": [120.0, 88.4], "to": "U2.6", "net": "/F_CLK"}]}),
            ])
        self.assertNotEqual(code1, 0)
        p1 = json.loads(out1.getvalue())
        self.assertEqual(p1["status"], "bad_request")
        self.assertIn("to_point", p1["reason"])

        out2 = io.StringIO()
        with mock.patch("sys.stdout", out2):
            code2 = lay.main([
                "batch", "--json", json.dumps({"routes": [{"from": "U2.6", "to": [120.0, 88.4], "net": "/F_CLK"}]}),
            ])
        self.assertNotEqual(code2, 0)
        p2 = json.loads(out2.getvalue())
        self.assertEqual(p2["status"], "bad_request")
        self.assertIn("to_via", p2["reason"])
        self.assertIn("to_point", p2["reason"])

    def test_to_point_taps_existing_track_via_and_in_batch_trunk_and_rejects_missing_before_commit(self) -> None:
        trunk_track = kr.Item("old-1", "track", "/F_CLK", "F.Cu", (10.0, 10.0), (10.0, 14.0), 0.25)
        existing_via = kr.Item("old-via", "via", "/F_CLK", None, (18.0, 12.0), (18.0, 12.0), 0.6)
        pads = [
            self._hpad("U1.1", "/LATCH", 6.0, 10.0),
            self._hpad("U1.2", "/LATCH", 6.0, 14.0),
            self._hpad("U1.3", "/LATCH", 10.0, 12.0),
            self._hpad("U2.6", "/F_CLK", 14.0, 10.0),
            self._hpad("U2.7", "/F_CLK", 14.0, 12.0),
        ]

        b_track = _V27Board(pads, [trunk_track])
        res_track = lay.execute_batch(b_track, {
            "routes": [{"from": "U2.7", "to_point": [10.0, 12.0], "net": "/F_CLK", "layer": "F.Cu"}],
        })
        self.assertEqual(res_track["status"], "laid")
        self.assertEqual(res_track["routes"][0]["to_point"], [10.0, 12.0])
        self.assertEqual(res_track["routes"][0]["end_layer"], "F.Cu")

        b_via = _V27Board(pads, [existing_via])
        res_via = lay.execute_batch(b_via, {
            "routes": [{"from": "U2.7", "to_point": [18.0, 12.0], "net": "/F_CLK", "layer": "F.Cu"}],
        })
        self.assertEqual(res_via["status"], "laid")

        b_batch = _V27Board(pads)
        res_batch = lay.execute_batch(b_batch, {
            "routes": [
                {"from": "U1.1", "to": "U1.2", "net": "/LATCH", "layer": "F.Cu"},
                {"from": "U1.3", "to_point": [6.0, 12.0], "net": "/LATCH", "layer": "F.Cu"},
            ],
        })
        self.assertEqual(res_batch["status"], "laid")
        self.assertEqual(b_batch.create_calls, 1)

        for miss_desc, miss_items, miss_pt in [
            ("empty space", [trunk_track], [20.0, 20.0]),
            ("wrong layer", [kr.Item("b-1", "track", "/F_CLK", "B.Cu", (10.0, 10.0), (10.0, 14.0), 0.25)], [10.0, 12.0]),
            ("wrong net", [kr.Item("o-1", "track", "/OTHER", "F.Cu", (10.0, 10.0), (10.0, 14.0), 0.25)], [10.0, 12.0]),
        ]:
            with self.subTest(miss=miss_desc):
                b_miss = _V27Board(pads, miss_items)
                with self.assertRaises(lay.LayFailure) as ctx:
                    lay.execute_batch(b_miss, {
                        "routes": [{"from": "U2.7", "to_point": miss_pt, "net": "/F_CLK", "layer": "F.Cu"}],
                    })
                self.assertEqual(ctx.exception.status, "no_target_copper")
                self.assertEqual(b_miss.create_calls, 0)


class _V28Board(_V27Board):
    def __init__(self, pads, items=None, *, clearance_mm=0.15, dru_floor=None, pad_obs=None):
        super().__init__(pads, items)
        self._clearance_mm = clearance_mm
        self._dru_floor = dru_floor
        self._pad_obs = list(pad_obs) if pad_obs is not None else [
            {
                "name": p.name,
                "net": p.net,
                "center": (p.x, p.y),
                "size": p.size or (0.8, 0.4),
                "angle_deg": float(getattr(p, "angle", 0.0) or 0.0),
                "shape": getattr(p.copper, "shape", "rect") if p.copper else "rect",
                "round_ratio": float(getattr(p.copper, "round_ratio", 0.0) or 0.0) if p.copper else 0.0,
                "layers": p.layers,
            }
            for p in pads
        ]

    def netclass_defaults(self, _net):
        return {
            "track_width_mm": 0.25,
            "via_diameter_mm": 0.6,
            "via_drill_mm": 0.3,
            "clearance_mm": self._clearance_mm,
        }

    def pad_obstacles(self):
        return list(self._pad_obs)

    def min_clearance_floor_mm(self):
        return self._dru_floor


class V28ObstaclePrecheckTests(unittest.TestCase):
    def test_dist_to_pad_shapes_and_rotation(self) -> None:
        rot_rect = {
            "center": (10.0, 10.0), "size": (1.6, 0.4), "angle_deg": 90.0,
            "shape": "rect", "round_ratio": 0.0, "layers": ("F.Cu",),
        }
        # Rotated 90 deg: extends +-0.2 in X and +-0.8 in Y.
        self.assertEqual(lay._dist_to_pad("via", (10.0, 10.6), (10.0, 10.6), rot_rect), 0.0)
        self.assertAlmostEqual(lay._dist_to_pad("via", (10.45, 10.0), (10.45, 10.0), rot_rect), 0.25, places=4)

        rr = {
            "center": (0.0, 0.0), "size": (2.0, 2.0), "angle_deg": 0.0,
            "shape": "roundrect", "round_ratio": 0.25, "layers": ("F.Cu",),
        }
        # Core half = 0.5, corner radius rc = 0.5 -> diagonal corner distance from (0.9, 0.9) is hypot(0.4, 0.4) - 0.5
        self.assertAlmostEqual(
            lay._dist_to_pad("via", (0.9, 0.9), (0.9, 0.9), rr),
            math.hypot(0.4, 0.4) - 0.5,
            places=4,
        )

        circ = {
            "center": (0.0, 0.0), "size": (1.0, 1.0), "angle_deg": 0.0,
            "shape": "circle", "round_ratio": 0.0, "layers": ("F.Cu",),
        }
        self.assertAlmostEqual(lay._dist_to_pad("track", (1.0, -2.0), (1.0, 2.0), circ), 0.5, places=4)

    def test_dru_min_clearance_mm_parses_comments_and_units(self) -> None:
        dru = """# Header comment
(version 1)
(rule "hv" (condition "A.NetName == 'HV'") (constraint clearance (min 0.5mm)))
(rule "neck" (condition "A.intersectsCourtyard('U*')") (constraint clearance (min 0.12 mm)))
(rule "fine" (condition "A.NetName == '/X'") (constraint clearance (min 4mil)))
"""
        self.assertAlmostEqual(lay._dru_min_clearance_mm(dru), 0.1016, places=4)
        self.assertIsNone(lay._dru_min_clearance_mm("# empty only\n(version 1)\n"))

    def test_obstacle_precheck_catches_collisions_before_create_and_honors_dru_floor(self) -> None:
        pads = [
            V27LayTests._hpad("U1.1", "/DATA_C", 8.0, 10.0),
            V27LayTests._hpad("U2.1", "/DATA_C", 20.0, 10.0),
            V27LayTests._hpad("U3.1", "+5V", 12.0, 20.0),
            V27LayTests._hpad("U3.2", "+5V", 16.0, 20.0),
        ]

        # 1. Existing same-layer track crossing -> obstacle_collision, zero create_copper calls.
        b_track = _V28Board(pads, [kr.Item("t1", "track", "Net-(U6-Q6)", "F.Cu", (14.0, 8.0), (14.0, 12.0), 0.25)])
        with self.assertRaises(lay.LayFailure) as ctx:
            lay.execute_batch(b_track, {"routes": [{"from": "U1.1", "to": "U2.1", "net": "/DATA_C", "layer": "F.Cu"}]})
        self.assertEqual(ctx.exception.status, "obstacle_collision")
        self.assertEqual(b_track.create_calls, 0)
        self.assertIn("existing Net-(U6-Q6) F.Cu track", ctx.exception.reason)

        # 2. Existing via hit from B.Cu route -> caught across layers before commit.
        tht_pads = [
            kr.PadInfo("U1.1", "/DATA_C", 8.0, 10.0, ("F.Cu", "B.Cu"), 0.2, True, (1.6, 0.4), copper=pads[0].copper),
            kr.PadInfo("U2.1", "/DATA_C", 20.0, 10.0, ("F.Cu", "B.Cu"), 0.2, True, (1.6, 0.4), copper=pads[1].copper),
        ]
        b_via = _V28Board(tht_pads, [kr.Item("v1", "via", "+5V", None, (12.0, 10.0), (12.0, 10.0), 0.6)])
        with self.assertRaises(lay.LayFailure) as ctx_v:
            lay.execute_batch(b_via, {"routes": [{"from": "U1.1", "to": "U2.1", "net": "/DATA_C", "layer": "B.Cu"}]})
        self.assertEqual(ctx_v.exception.status, "obstacle_collision")
        self.assertEqual(b_via.create_calls, 0)
        self.assertIn("existing +5V via at (12, 10)", ctx_v.exception.reason)

        # 3. Different-net (or unconnected "") pad hit on same layer.
        pad_block = {"name": "J9.1", "net": "", "center": (14.0, 10.0), "size": (1.0, 1.0), "angle_deg": 0.0, "shape": "rect", "round_ratio": 0.0, "layers": ("F.Cu",)}
        b_pad = _V28Board(pads, [], pad_obs=b_via._pad_obs + [pad_block])
        with self.assertRaises(lay.LayFailure) as ctx_p:
            lay.execute_batch(b_pad, {"routes": [{"from": "U1.1", "to": "U2.1", "net": "/DATA_C", "layer": "F.Cu"}]})
        self.assertEqual(ctx_p.exception.status, "obstacle_collision")
        self.assertEqual(b_pad.create_calls, 0)
        self.assertIn("pad J9.1 (<unconnected>)", ctx_p.exception.reason)

        # 4. Clearance violation (gap=0.130 mm < 0.150 mm) caught, and honored when dru_floor=0.12 allows 0.130 mm.
        # Planned track width 0.25 (half 0.125) at y=10.0, neighbor track width 0.25 (half 0.125) at y=10.38 -> gap = 0.38 - 0.25 = 0.130 mm.
        near_track = kr.Item("t2", "track", "+5V", "F.Cu", (10.0, 10.38), (18.0, 10.38), 0.25)
        b_cl = _V28Board(pads, [near_track], clearance_mm=0.15, dru_floor=None)
        with self.assertRaises(lay.LayFailure) as ctx_c:
            lay.execute_batch(b_cl, {"routes": [{"from": "U1.1", "to": "U2.1", "net": "/DATA_C", "layer": "F.Cu"}]})
        self.assertEqual(ctx_c.exception.status, "obstacle_collision")
        self.assertEqual(b_cl.create_calls, 0)
        self.assertIn("gap=0.130 mm, need 0.150 mm", ctx_c.exception.reason)

        b_dru = _V28Board(pads, [near_track], clearance_mm=0.15, dru_floor=0.12)
        res_dru = lay.execute_batch(b_dru, {"routes": [{"from": "U1.1", "to": "U2.1", "net": "/DATA_C", "layer": "F.Cu"}]})
        self.assertEqual(res_dru["status"], "laid")
        self.assertEqual(b_dru.create_calls, 1)

        # 5. Intra-batch different-net crossing caught before commit.
        cross_pads = [
            V27LayTests._hpad("U1.1", "/DATA_C", 8.0, 10.0),
            V27LayTests._hpad("U2.1", "/DATA_C", 20.0, 10.0),
            V27LayTests._hpad("U4.1", "+5V", 14.0, 6.0, size=(0.4, 1.6)),
            V27LayTests._hpad("U4.2", "+5V", 14.0, 14.0, size=(0.4, 1.6)),
        ]
        b_intra = _V28Board(cross_pads, [])
        with self.assertRaises(lay.LayFailure) as ctx_i:
            lay.execute_batch(b_intra, {
                "routes": [
                    {"from": "U1.1", "to": "U2.1", "net": "/DATA_C", "layer": "F.Cu"},
                    {"from": "U4.1", "to": "U4.2", "net": "+5V", "layer": "F.Cu"},
                ],
            })
        self.assertEqual(ctx_i.exception.status, "obstacle_collision")
        self.assertEqual(b_intra.create_calls, 0)
        self.assertIn("batch +5V", ctx_i.exception.reason)

        # 6. Different-layer track (B.Cu existing vs F.Cu planned) passes cleanly.
        b_other_layer = _V28Board(pads, [kr.Item("tb", "track", "+5V", "B.Cu", (14.0, 8.0), (14.0, 12.0), 0.25)])
        res_layer = lay.execute_batch(b_other_layer, {"routes": [{"from": "U1.1", "to": "U2.1", "net": "/DATA_C", "layer": "F.Cu"}]})
        self.assertEqual(res_layer["status"], "laid")
        self.assertEqual(b_other_layer.create_calls, 1)


class V281IpcCachingTests(unittest.TestCase):
    def test_kipylayboard_caches_footprints_layers_and_bulk_netclasses_across_multi_route_batch(self) -> None:
        from types import SimpleNamespace

        class _FakeEnum:
            PSS_RECTANGLE = 1
            PSS_ROUNDRECT = 2
            PSS_OVAL = 3
            PSS_CIRCLE = 4

        class _FakeProtoTypes:
            PadStackShape = _FakeEnum

        class _FakeKipy:
            pass

        def make_vec(x_mm, y_mm):
            return SimpleNamespace(x=int(round(x_mm * lay.NM)), y=int(round(y_mm * lay.NM)))

        def make_fp_pad(num, net_name, x_mm, y_mm):
            return SimpleNamespace(
                number=num,
                net=SimpleNamespace(name=net_name),
                position=make_vec(x_mm, y_mm),
                padstack=SimpleNamespace(
                    layers=["F.Cu"],
                    angle=SimpleNamespace(degrees=0.0),
                    copper_layers=[SimpleNamespace(
                        size=make_vec(1.0, 0.4),
                        offset=make_vec(0.0, 0.0),
                        shape=_FakeEnum.PSS_RECTANGLE,
                        corner_rounding_ratio=0.0,
                    )],
                ),
            )

        fps = [
            SimpleNamespace(
                reference_field=SimpleNamespace(text=SimpleNamespace(value="U1")),
                definition=SimpleNamespace(pads=[
                    make_fp_pad("1", "/N1", 10.0, 10.0),
                    make_fp_pad("2", "/N2", 10.0, 12.0),
                    make_fp_pad("3", "/N3", 10.0, 14.0),
                ]),
            ),
            SimpleNamespace(
                reference_field=SimpleNamespace(text=SimpleNamespace(value="U2")),
                definition=SimpleNamespace(pads=[
                    make_fp_pad("1", "/N1", 20.0, 10.0),
                    make_fp_pad("2", "/N2", 20.0, 12.0),
                    make_fp_pad("3", "/N3", 20.0, 14.0),
                ]),
            ),
        ]
        nets = [SimpleNamespace(name="/N1"), SimpleNamespace(name="/N2"), SimpleNamespace(name="/N3"), SimpleNamespace(name="/OBS")]
        nc_info = SimpleNamespace(
            track_width=int(0.25 * lay.NM),
            via_diameter=int(0.6 * lay.NM),
            via_drill=int(0.3 * lay.NM),
            clearance=int(0.15 * lay.NM),
        )

        counts = {"footprints": 0, "layers": 0, "nets": 0, "netclasses": 0, "begin_commit": 0}

        class _FakeKipyBoard:
            def get_active_layer(self):
                return "F.Cu"

            def get_enabled_layers(self):
                counts["layers"] += 1
                return ["F.Cu", "B.Cu"]

            def get_footprints(self):
                counts["footprints"] += 1
                return fps

            def get_nets(self):
                counts["nets"] += 1
                return nets

            def get_netclass_for_nets(self, arg):
                counts["netclasses"] += 1
                seq = arg if isinstance(arg, list) else [arg]
                return {n.name: nc_info for n in seq}

            def get_tracks(self):
                return [SimpleNamespace(
                    id=SimpleNamespace(value="obs-1"),
                    net=SimpleNamespace(name="/OBS"),
                    layer="F.Cu",
                    start=make_vec(15.0, 8.0),
                    end=make_vec(15.0, 16.0),
                    width=int(0.25 * lay.NM),
                )]

            def get_vias(self):
                return []

            def begin_commit(self):
                counts["begin_commit"] += 1
                return object()

        fake_board = _FakeKipyBoard()
        route = kr.KipyBoard.__new__(kr.KipyBoard)
        route.canonical_name = lambda x: str(x)
        route.layer_id = lambda x: str(x)
        route.is_copper = lambda x: str(x).endswith(".Cu")
        route.board = fake_board
        route.name = "cache_test"
        route.project = "/tmp/cache_test"

        kboard = lay.KipyLayBoard.__new__(lay.KipyLayBoard)
        kboard.route = route
        kboard.board = fake_board
        kboard._init_caches()

        import sys
        import types
        from unittest import mock

        kipy_pkg = types.ModuleType("kipy")
        kipy_proto = types.ModuleType("kipy.proto")
        kipy_proto_board = types.ModuleType("kipy.proto.board")
        kipy_proto_board.board_types_pb2 = _FakeProtoTypes
        with (
            mock.patch.dict(sys.modules, {
                "kipy": kipy_pkg,
                "kipy.proto": kipy_proto,
                "kipy.proto.board": kipy_proto_board,
                "kipy.proto.board.board_types_pb2": _FakeProtoTypes,
            }),
            mock.patch.object(kboard, "min_clearance_floor_mm", return_value=None),
        ):
            with self.assertRaises(lay.LayFailure) as ctx:
                lay.execute_batch(kboard, {
                    "routes": [
                        {"from": "U1.1", "to": "U2.1", "net": "/N1", "layer": "F.Cu"},
                        {"from": "U1.2", "to": "U2.2", "net": "/N2", "layer": "F.Cu"},
                        {"from": "U1.3", "to": "U2.3", "net": "/N3", "layer": "F.Cu"},
                    ]
                })
            self.assertEqual(ctx.exception.status, "obstacle_collision")
            self.assertIn("existing /OBS F.Cu track", ctx.exception.reason)

        self.assertEqual(counts["footprints"], 1)
        self.assertEqual(counts["layers"], 1)
        self.assertEqual(counts["nets"], 1)
        self.assertEqual(counts["netclasses"], 1)
        self.assertEqual(counts["begin_commit"], 0)


if __name__ == "__main__":
    unittest.main()
