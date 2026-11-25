#!/usr/bin/env python3
"""Checked atomic IPC copper laying in the open PCB Editor on the Mac.

  .venv-kicad-ipc/bin/python scripts/kicad_lay.py pads J1.1 J2.1 [--facing J2.1]
  .venv-kicad-ipc/bin/python scripts/kicad_lay.py route J1.1 J2.1 --layer F.Cu --width 0.25 --centered-exits
  .venv-kicad-ipc/bin/python scripts/kicad_lay.py batch --json '{"routes":[...]}' [--no-drc]
  .venv-kicad-ipc/bin/python scripts/kicad_lay.py batch --stdin <<'EOF' ... EOF

Reads pad geometry, net assignments and netclasses from KiCad over IPC, expands
45-degree waypoints, validates pad exits and geometry before editing, creates
tracks and vias in KiCad, checks live route quality, connectivity and DRC, and
rolls back automatically if any check fails. Never saves the board or imports
arbitrary user code. Prints one JSON result and exits 0 only when `ok`.
"""
from __future__ import annotations

import argparse
import json
import math
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import kicad_ipc  # noqa: E402
import kicad_lay_plan as _plan  # noqa: E402
import kicad_route as kr  # noqa: E402
import kicad_rules_geom as rg  # noqa: E402
import mac  # noqa: E402
import pad_exits  # noqa: E402
from kicad_lay_plan import (  # noqa: E402
    DEFAULT_ESCAPE_MM,
    DEFAULT_TRACK_MM,
    DEFAULT_VIA_DIA_MM,
    DEFAULT_VIA_DRILL_MM,
    DEFAULT_ZONE_MIN_ISLAND_MM2,
    DEFAULT_ZONE_MIN_THICKNESS_MM,
    ENDPOINT_EPS_MM,
    ZONE_CONNECT_MODES,
    ZONE_ISLAND_MODES,
    PlannedPour,
    PlannedSegment,
    PlannedVia,
    _corner_radius,
    _corner_score,
    _drc_error_counts,
    _is_finite_number,
    _normalize_step,
    _plan_route_geometry,
    _point,
    _prevalidate_batch_spec,
    _prevalidate_route_spec,
    _step_width,
    _valid_pad_ref,
    _validate_route_endpoints,
    expand_45_leg,
    facing_escape,
    merge_collinear,
    new_drc_errors,
    plan_pour,
    plan_route,
    plan_via,
    verify_centered_exits,
)
from kicad_route_quality import ANGLE_TOLERANCE_MM, audit_live_board, compass_direction  # noqa: E402
from kicad_rules_geom import (  # noqa: E402
    NM,
    LayFailure,
    _fmt_num,
    axis_aligned_rect_bounds as _axis_aligned_rect_bounds,
    clean_ring as _clean_ring,
    dist_point_to_aabb as _dist_point_to_aabb,
    dist_point_to_seg as _dist_point_to_seg,
    dist_seg_to_aabb as _dist_seg_to_aabb,
    dist_seg_to_seg as _dist_seg_to_seg,
    dist_to_pad as _dist_to_pad,
    dru_min_clearance_mm as _dru_min_clearance_mm,
    extract_outline_polygon,
    inset_polygon,
    point_in_filled_zone,
    point_in_ring,
    polygon_signed_area,
    same_pt,
    sample_arc_points,
    segments_cross as _segments_cross,
    snap_pt,
    stitch_outline_edges,
    to_mm,
    to_nm,
    validate_polygon,
)

TOOL_ROOT = kicad_ipc.TOOL_ROOT


class KipyLayBoard(kicad_ipc.KipyBoardCacheMixin):
    """Live KiCad IPC adapter reusing kicad_route.KipyBoard."""

    def __init__(self, pid: str | None = None):
        window = kr.pick_editor(pid)
        own = pathlib.Path(f"/tmp/kicad/api-{window['pid']}.sock")
        if not own.exists():
            editors = [w for w in mac.windows(every=True) if w["app"] == "PCB Editor"]
            if len({w["pid"] for w in editors}) > 1:
                raise LayFailure("which_editor", f"pid {window['pid']} has no unique IPC socket beside other PCB Editors")
        self.route = kr.KipyBoard(str(window["pid"]))
        self.board = self.route.board
        self.wait_ready()
        if not window["title"].lstrip("*").startswith(f"{self.route.name} "):
            raise LayFailure("which_editor", f'IPC answers for {self.route.name}, but window is "{window["title"]}"')
        self._init_caches()

    def _init_caches(self) -> None:
        self.init_ipc_caches()

    def wait_ready(self) -> None:
        self.route.wait_ready()

    def file(self) -> str:
        return str(pathlib.PurePosixPath(self.board.document.project.path) / self.board.document.board_filename)

    def _all_footprints(self) -> list:
        return self.all_footprints()

    def pad(self, spec: str) -> kr.PadInfo:
        name, near = kr.split_pad_spec(spec)
        reference, _, number = name.rpartition(".")
        fps = self.footprints_for_ref(reference)
        if not fps:
            raise kr.RouteFailure("bad_request", f"no footprint {reference} on {self.route.name}")
        found = [
            self.route._pad_info(spec, fp, pad)
            for fp in fps
            for pad in fp.definition.pads
            if pad.number == number
        ]
        if not found:
            raise kr.RouteFailure("bad_request", f"{reference} has no pad {number}")
        return kr.choose_pad(name, near, found)

    def nets(self) -> set[str]:
        return set(self._net_map())

    def board_outline(self) -> dict:
        self.wait_ready()
        return extract_outline_polygon(self.board.get_shapes(), canonical_layer=self.route.canonical_name)

    def _polyline_points(self, polyline) -> list[list[float]]:
        pts: list[list[float]] = []
        for node in polyline:
            if getattr(node, "has_point", False):
                pts.append([to_mm(node.point.x), to_mm(node.point.y)])
            elif getattr(node, "has_arc", False):
                a = node.arc
                for pt in sample_arc_points(
                    (to_mm(a.start.x), to_mm(a.start.y)),
                    (to_mm(a.mid.x), to_mm(a.mid.y)),
                    (to_mm(a.end.x), to_mm(a.end.y)),
                ):
                    pts.append([pt[0], pt[1]])
        return pts

    def _zone_to_dict(self, z) -> dict:
        from kipy.proto.board import board_types_pb2 as pb
        zid = str(z.id.value) if getattr(z, "id", None) and z.id.value else ""
        if z.is_rule_area():
            ztype = "rule_area"
        elif z.type == pb.ZoneType.ZT_COPPER:
            ztype = "copper"
        else:
            ztype = "other"
        layers = [self.route.canonical_name(layer) for layer in z.layers]
        outline_mm: list[list[float]] = []
        holes_mm: list[list[list[float]]] = []
        if len(z._proto.outline.polygons) > 0:
            pwh = z.outline
            outline_mm = self._polyline_points(pwh.outline)
            holes_mm = [self._polyline_points(h) for h in pwh.holes]
        conn_map = {
            pb.ZoneConnectionStyle.ZCS_THERMAL: "thermal",
            pb.ZoneConnectionStyle.ZCS_FULL: "solid",
            pb.ZoneConnectionStyle.ZCS_PTH_THERMAL: "pth_thermal",
            pb.ZoneConnectionStyle.ZCS_NONE: "none",
            pb.ZoneConnectionStyle.ZCS_INHERITED: "inherited",
        }
        island_map = {
            pb.IslandRemovalMode.IRM_ALWAYS: "always",
            pb.IslandRemovalMode.IRM_AREA: "area",
            pb.IslandRemovalMode.IRM_NEVER: "never",
        }
        filled_layers = sorted(
            self.route.canonical_name(layer)
            for layer, polys in z.filled_polygons.items()
            if polys
        )
        spokes = z._proto.copper_settings.connection.thermal_spokes
        return {
            "id": zid,
            "name": z.name,
            "type": ztype,
            "net": "" if z.is_rule_area() else (z.net.name if z.net else ""),
            "layers": layers,
            "priority": int(z.priority),
            "clearance_mm": None if z.is_rule_area() or z.clearance is None else to_mm(z.clearance),
            "min_thickness_mm": None if z.is_rule_area() or z.min_thickness is None else to_mm(z.min_thickness),
            "connect": None if z.is_rule_area() else conn_map.get(z._proto.copper_settings.connection.zone_connection, "thermal"),
            "thermal_gap_mm": None if z.is_rule_area() or not spokes.HasField("gap") else to_mm(spokes.gap.value_nm),
            "thermal_bridge_width_mm": None if z.is_rule_area() or not spokes.HasField("width") else to_mm(spokes.width.value_nm),
            "island_mode": None if z.is_rule_area() else island_map.get(z.island_mode, "always"),
            "outline_mm": outline_mm,
            "holes_mm": holes_mm,
            "filled": bool(filled_layers),
            "filled_layers": filled_layers,
            "locked": bool(z.locked),
        }

    def zones(self) -> list[dict]:
        self._raw_zones = {}
        out: list[dict] = []
        for z in self.raw_zones():
            info = self._zone_to_dict(z)
            if info["id"]:
                self._raw_zones[info["id"]] = z
            out.append(info)
        return out

    def filled_zone_polygons(
        self, net_name: str, layers: tuple[str, ...] | None = None
    ) -> dict[str, list[tuple[list[list[float]], list[list[list[float]]]]]]:
        from kipy.proto.board import board_types_pb2 as pb

        wanted = set(layers) if layers else None
        by_layer: dict[str, list[tuple[list[list[float]], list[list[list[float]]]]]] = {}
        for z in self.raw_zones():
            if z.is_rule_area() or z.type != pb.ZoneType.ZT_COPPER:
                continue
            z_net = z.net.name if z.net else ""
            if z_net != net_name:
                continue
            for layer, polys in z.filled_polygons.items():
                if not polys:
                    continue
                cname = self.route.canonical_name(layer)
                if wanted is not None and cname not in wanted:
                    continue
                out_list = by_layer.setdefault(cname, [])
                for pwh in polys:
                    outline = self._polyline_points(pwh.outline)
                    if len(outline) < 3:
                        continue
                    holes = [pts for h in pwh.holes for pts in (self._polyline_points(h),) if len(pts) >= 3]
                    out_list.append((outline, holes))
        return by_layer

    def enabled_layers(self) -> tuple[str, ...]:
        self._ensure_ipc_caches()
        if self._enabled_layers is None:
            raw_enabled = self.enabled_layer_ids()
            self._enabled_layers = tuple(
                self.route.canonical_name(layer) for layer in raw_enabled if self.route.is_copper(layer)
            )
        return self._enabled_layers

    def _net_map(self) -> dict:
        return self.net_map()

    @staticmethod
    def _extract_netclass(info) -> dict:
        return kicad_ipc.KipyBoardCacheMixin._extract_netclass_dict(info)

    def items(self) -> list[kr.Item]:
        raw_tracks, raw_vias = self.raw_tracks_and_vias()
        out = []
        for track in raw_tracks:
            out.append(kr.Item(
                str(track.id.value),
                "track",
                track.net.name,
                self.route.canonical_name(track.layer),
                (track.start.x / NM, track.start.y / NM),
                (track.end.x / NM, track.end.y / NM),
                track.width / NM,
            ))
        for via in raw_vias:
            at = (via.position.x / NM, via.position.y / NM)
            out.append(kr.Item(str(via.id.value), "via", via.net.name, None, at, at, via.diameter / NM))
        return out

    def pad_obstacles(self) -> list[dict]:
        """Oriented copper geometry and layers for every pad on the live board."""
        self._ensure_ipc_caches()
        if self._pad_obstacles is not None:
            return self._pad_obstacles
        from kipy.proto.board.board_types_pb2 import PadStackShape

        names = {
            PadStackShape.PSS_RECTANGLE: "rect",
            PadStackShape.PSS_ROUNDRECT: "roundrect",
            PadStackShape.PSS_OVAL: "oval",
            PadStackShape.PSS_CIRCLE: "circle",
        }
        out: list[dict] = []
        for footprint in self._all_footprints():
            ref = footprint.reference_field.text.value if getattr(footprint, "reference_field", None) else ""
            for pad in footprint.definition.pads:
                info = kicad_ipc.extract_pad_geometry(
                    pad, self.route.canonical_name, self.route.copper_here,
                    shape_names=names, default_shape="rect",
                )
                if info is None:
                    continue
                out.append({
                    "name": f"{ref}.{pad.number}" if ref else str(pad.number),
                    "net": info["net"],
                    "center": info["center"],
                    "size": info["size"],
                    "angle_deg": info["angle_deg"],
                    "shape": info["shape"],
                    "round_ratio": info["round_ratio"],
                    "layers": info["layers"],
                })
        self._pad_obstacles = out
        return out

    def min_clearance_floor_mm(self) -> float | None:
        """Smallest positive `.kicad_dru` custom clearance rule (so local relaxations never false-positive)."""
        self._ensure_ipc_caches()
        loaded, val = self._clearance_floor
        if loaded:
            return val
        project = pathlib.Path(self.route.project)
        dru_path = project / f"{self.route.name}.kicad_dru"
        found: float | None = None
        if dru_path.is_file():
            try:
                found = _dru_min_clearance_mm(dru_path.read_text(encoding="utf-8"))
            except OSError:
                found = None
        self._clearance_floor = (True, found)
        return found

    def quality(self) -> dict[str, list[str]]:
        fps = self._all_footprints()
        raw_tracks, raw_vias = self.raw_tracks_and_vias()
        findings, _ = audit_live_board(
            pathlib.Path(self.route.project),
            self.board,
            footprints=fps,
            tracks=raw_tracks,
            vias=raw_vias,
        )
        errors: dict[tuple, str] = {}
        warnings: dict[tuple, str] = {}
        for item in findings:
            key = (item.severity, item.rule, item.net, item.layer, item.point_mm, item.message)
            text = f"{item.severity}: {item.rule} on {item.net} at {item.point_mm}: {item.message}"
            if item.severity == "error":
                errors[key] = text
            elif item.severity == "warning":
                warnings[key] = text
        return {"errors": errors, "warnings": warnings}

    def text(self) -> str:
        return self.board.get_as_string()

    def run_drc_diff(self, before_text: str, after_text: str) -> dict:
        project = pathlib.Path(self.route.project)
        cli = kicad_ipc.invoke_kicad_cli_path()
        reports = []
        with tempfile.TemporaryDirectory(prefix="board-graph-lay-drc-") as folder:
            for suffix in (".kicad_pro", ".kicad_dru"):
                src = project / f"{self.route.name}{suffix}"
                if src.exists():
                    shutil.copy(src, pathlib.Path(folder) / src.name)
            for label, content in (("before", before_text), ("after", after_text)):
                pcb = pathlib.Path(folder) / f"{self.route.name}.kicad_pcb"
                pcb.write_text(content, encoding="utf-8")
                out = pathlib.Path(folder) / f"{label}.json"
                done = subprocess.run(
                    [sys.executable, str(cli), "pcb", "drc", "--format", "json", "--severity-all",
                     "--refill-zones", "--output", str(out), str(pcb)],
                    capture_output=True, text=True, check=False, timeout=600,
                )
                if not out.is_file():
                    raise LayFailure("drc_failed", "kicad-cli DRC failed: " + (done.stderr or done.stdout).strip()[-300:])
                reports.append(json.loads(out.read_text(encoding="utf-8")))
        added_errors = new_drc_errors(reports[0], reports[1])
        return {
            "new_errors": added_errors,
            "unconnected": [len(reports[0].get("unconnected_items", [])), len(reports[1].get("unconnected_items", []))],
        }

    def _build_zone(self, pour: PlannedPour, nets: dict):
        import kipy.board_types as bt
        from kipy import geometry as geom
        from kipy.proto.board import board_types_pb2 as pb

        net_obj = nets.get(pour.net)
        if net_obj is None:
            raise LayFailure("bad_request", f"unknown net {pour.net!r} on board")
        zone = bt.Zone()
        zone.type = pb.ZoneType.ZT_COPPER
        zone.net = net_obj
        zone.layers = [self.route.layer_id(layer) for layer in pour.layers]
        if pour.name:
            zone.name = pour.name
        zone.priority = pour.priority
        if pour.clearance is not None:
            zone.clearance = to_nm(pour.clearance)
        zone.min_thickness = to_nm(pour.min_thickness)
        zone._proto.copper_settings.fill_mode = pb.ZoneFillMode.ZFM_SOLID
        conn_styles = {
            "thermal": pb.ZoneConnectionStyle.ZCS_THERMAL,
            "solid": pb.ZoneConnectionStyle.ZCS_FULL,
            "pth_thermal": pb.ZoneConnectionStyle.ZCS_PTH_THERMAL,
            "none": pb.ZoneConnectionStyle.ZCS_NONE,
        }
        zone._proto.copper_settings.connection.zone_connection = conn_styles[pour.connect]
        if pour.thermal_gap is not None:
            zone._proto.copper_settings.connection.thermal_spokes.gap.value_nm = to_nm(pour.thermal_gap)
        if pour.thermal_bridge_width is not None:
            zone._proto.copper_settings.connection.thermal_spokes.width.value_nm = to_nm(pour.thermal_bridge_width)
        island_styles = {
            "always": pb.IslandRemovalMode.IRM_ALWAYS,
            "area": pb.IslandRemovalMode.IRM_AREA,
            "never": pb.IslandRemovalMode.IRM_NEVER,
        }
        zone.island_mode = island_styles[pour.island_mode]
        zone.min_island_area = int(round(pour.min_island_area * NM * NM))

        pwh = geom.PolygonWithHoles()
        outline_poly = geom.PolyLine()
        for x, y in pour.outline:
            outline_poly.append(geom.PolyLineNode.from_xy(to_nm(x), to_nm(y)))
        outline_poly.closed = True
        pwh.outline = outline_poly
        for hole in pour.holes:
            hole_poly = geom.PolyLine()
            for x, y in hole:
                hole_poly.append(geom.PolyLineNode.from_xy(to_nm(x), to_nm(y)))
            hole_poly.closed = True
            pwh.add_hole(hole_poly)
        zone.outline = pwh
        return zone

    def create_copper(self, segments: list[PlannedSegment], vias: list[PlannedVia], message: str, *,
                      pours: tuple[PlannedPour, ...] | list[PlannedPour] = (),
                      replace_zone_ids: tuple[str, ...] | list[str] = (),
                      refill_zones: bool = False,
                      run_drc: bool = True) -> dict:
        import kipy.board_types as bt
        from kipy.geometry import Vector2

        self.wait_ready()
        nets = self._net_map()
        objs = []
        for seg in segments:
            net_obj = nets.get(seg.net)
            if net_obj is None:
                raise LayFailure("bad_request", f"unknown net {seg.net!r} on board")
            track = bt.Track()
            track.net = net_obj
            track.layer = self.route.layer_id(seg.layer)
            track.width = to_nm(seg.width)
            track.start = Vector2.from_xy(to_nm(seg.start[0]), to_nm(seg.start[1]))
            track.end = Vector2.from_xy(to_nm(seg.end[0]), to_nm(seg.end[1]))
            objs.append(track)
        for v in vias:
            net_obj = nets.get(v.net)
            if net_obj is None:
                raise LayFailure("bad_request", f"unknown net {v.net!r} on board")
            via = bt.Via()
            via.net = net_obj
            via.position = Vector2.from_xy(to_nm(v.at[0]), to_nm(v.at[1]))
            via.diameter = to_nm(v.diameter)
            via.drill_diameter = to_nm(v.drill)
            objs.append(via)
        for pour in pours:
            objs.append(self._build_zone(pour, nets))

        has_zones = bool(pours or replace_zone_ids)
        need_refill = has_zones or refill_zones
        before_zones_raw = list(self.board.get_zones()) if has_zones else []
        before_zone_ids = {str(z.id.value) for z in before_zones_raw if getattr(z, "id", None) and z.id.value}
        victims = []
        replaced_snapshots = []
        if replace_zone_ids:
            wanted_replace = set(replace_zone_ids)
            raw_by_id = {str(z.id.value): z for z in before_zones_raw if getattr(z, "id", None) and z.id.value}
            missing = sorted(wanted_replace - set(raw_by_id))
            if missing:
                raise LayFailure("not_found", f"zone(s) to replace disappeared: {', '.join(missing)}")
            for zid in replace_zone_ids:
                rz = raw_by_id[zid]
                if rz.is_rule_area():
                    raise LayFailure("forbidden", f"zone {zid} is a rule area")
                if rz.locked:
                    raise LayFailure("locked", f"zone {zid} is locked")
                victims.append(rz)
                snap = bt.Zone(proto=rz._proto)
                snap._proto.ClearField("id")
                replaced_snapshots.append(snap)

        before_items = self.items()
        before_ids = {item.id for item in before_items}
        before_q = self.quality()
        before_text = self.text() if run_drc else ""

        commit = self.board.begin_commit()
        pushed = False
        created_ids: set[str] = set()
        created_zone_ids: set[str] = set()
        try:
            if victims:
                self.board.remove_items(victims)
            created = self.board.create_items(objs) if objs else []
            self.invalidate_copper_cache()
            raw_created_ids = {str(item.id.value) for item in created if getattr(item, "id", None) and item.id.value}
            live_after_create = {item.id for item in self.items()}
            created_ids = raw_created_ids & live_after_create if raw_created_ids else set()
            if segments or vias:
                if not created_ids:
                    created_ids = live_after_create - before_ids
                if not created_ids.issubset(live_after_create) or need_refill or (run_drc and self.text() == before_text):
                    self.board.push_commit(commit, message)
                    pushed = True
                    self.invalidate_copper_cache()
                    if not created_ids:
                        created_ids = {item.id for item in self.items()} - before_ids
            else:
                self.board.push_commit(commit, message)
                pushed = True
                self.invalidate_copper_cache()

            if need_refill:
                self.board.refill_zones()
                self.invalidate_copper_cache()
                if has_zones:
                    surviving_before = before_zone_ids - set(replace_zone_ids)
                    after_zone_ids = {str(z.id.value) for z in self.raw_zones() if getattr(z, "id", None) and z.id.value}
                    created_zone_ids = after_zone_ids - surviving_before
            return {
                "before_ids": before_ids,
                "created_ids": created_ids,
                "created_zone_ids": created_zone_ids,
                "replaced_snapshots": replaced_snapshots,
                "has_zones": has_zones,
                "need_refill": need_refill,
                "before_quality": before_q,
                "before_text": before_text,
                "commit": commit,
                "pushed": pushed,
            }
        except Exception:
            if not pushed:
                try:
                    self.board.drop_commit(commit)
                except Exception:
                    pass
            restore = replaced_snapshots
            if has_zones and not created_zone_ids:
                try:
                    surviving_before = (before_zone_ids - set(replace_zone_ids)) if pushed else before_zone_ids
                    after_zone_ids = {
                        str(z.id.value)
                        for z in self.board.get_zones()
                        if getattr(z, "id", None) and z.id.value
                    }
                    created_zone_ids = after_zone_ids - surviving_before
                    if not pushed and set(replace_zone_ids).issubset(after_zone_ids):
                        restore = ()
                except Exception:
                    pass
            self._ensure_removed(created_ids, message + " (rollback)",
                                 created_zone_ids=created_zone_ids,
                                 restore_zones=restore,
                                 refill=need_refill)
            self.invalidate_copper_cache()
            raise

    def delete_zones(self, zone_ids: list[str], message: str, *, run_drc: bool = True) -> dict:
        import kipy.board_types as bt

        self.wait_ready()
        self.invalidate_copper_cache()
        raw_by_id = {str(z.id.value): z for z in self.raw_zones() if getattr(z, "id", None) and z.id.value}
        missing = [zid for zid in zone_ids if zid not in raw_by_id]
        if missing:
            raise LayFailure("not_found", f"zone(s) not found on board: {', '.join(missing)}")
        victims = []
        snapshots = []
        for zid in dict.fromkeys(zone_ids):
            rz = raw_by_id[zid]
            if rz.is_rule_area():
                raise LayFailure("forbidden", f"zone {zid} is a rule area; rule areas cannot be deleted")
            if rz.locked:
                raise LayFailure("locked", f"zone {zid} is locked and cannot be deleted")
            victims.append(rz)
            snap = bt.Zone(proto=rz._proto)
            snap._proto.ClearField("id")
            snapshots.append(snap)

        victim_ids = [str(z.id.value) for z in victims]
        before_text = self.text() if run_drc else ""
        commit = self.board.begin_commit()
        pushed = False
        try:
            self.board.remove_items(victims)
            self.board.push_commit(commit, message)
            pushed = True
            self.board.refill_zones()
            self.invalidate_copper_cache()
            drc_info = {"skipped": True}
            if run_drc:
                drc_info = self.run_drc_diff(before_text, self.text())
                if drc_info["new_errors"]:
                    first = drc_info["new_errors"][0]
                    raise LayFailure("drc_failed", f"new DRC error: {first.get('description')}",
                                     violations=drc_info["new_errors"], drc=drc_info)
            return {"deleted_ids": victim_ids, "drc": drc_info}
        except Exception:
            if not pushed:
                try:
                    self.board.drop_commit(commit)
                except Exception:
                    pass
            else:
                self._ensure_removed(set(), message + " (rollback)",
                                     created_zone_ids=set(), restore_zones=snapshots, refill=True)
            raise

    def finalize_commit(self, tx: dict, message: str) -> None:
        if not tx["pushed"]:
            self.board.push_commit(tx["commit"], message)
            tx["pushed"] = True

    def rollback_commit(self, tx: dict, message: str) -> None:
        if not tx["pushed"]:
            try:
                self.board.drop_commit(tx["commit"])
            except Exception:
                pass
        self._ensure_removed(
            tx["created_ids"],
            message + " (rollback)",
            created_zone_ids=tx.get("created_zone_ids", set()),
            restore_zones=tx.get("replaced_snapshots", ()),
            refill=bool(tx.get("need_refill") or tx.get("has_zones")),
        )

    def _ensure_removed(self, ids_to_remove: set[str], message: str, *,
                        created_zone_ids: set[str] = frozenset(),
                        restore_zones=(), refill: bool = False) -> None:
        if not ids_to_remove and not created_zone_ids and not restore_zones:
            return
        self.wait_ready()
        victims = []
        if ids_to_remove:
            victims.extend(t for t in self.board.get_tracks() if str(t.id.value) in ids_to_remove)
            victims.extend(v for v in self.board.get_vias() if str(v.id.value) in ids_to_remove)
        if created_zone_ids:
            victims.extend(z for z in self.board.get_zones() if str(z.id.value) in created_zone_ids)
        if not victims and not restore_zones:
            return
        rb = self.board.begin_commit()
        try:
            if victims:
                self.board.remove_items(victims)
            if restore_zones:
                self.board.create_items(list(restore_zones))
        except Exception:
            self.board.drop_commit(rb)
            raise
        self.board.push_commit(rb, message)
        if refill:
            self.board.refill_zones()
        self.invalidate_copper_cache()


def execute_pads(board, pad_specs: list[str], facing_spec: str | None = None) -> dict:
    """Read-only pad inspection with optional short-edge facing vectors."""
    pads = [board.pad(spec) for spec in pad_specs]
    facing_target = board.pad(facing_spec) if facing_spec else None
    out = []
    for idx, pad in enumerate(pads):
        copper = pad.copper
        info = {
            "pad": pad.name,
            "net": pad.net,
            "center_mm": list(snap_pt(copper.center if copper else (pad.x, pad.y))),
            "size_mm": [round(copper.size[0], 6), round(copper.size[1], 6)] if copper else None,
            "angle_deg": round(copper.angle_deg, 4) if copper else None,
            "shape": copper.shape if copper else None,
            "layers": list(pad.layers),
            "netclass": board.netclass_defaults(pad.net),
        }
        other = facing_target
        if other is None and len(pads) == 2 and idx in (0, 1):
            other = pads[1 - idx]
        if other is not None:
            oc = other.copper.center if other.copper else (other.x, other.y)
            try:
                esc = facing_escape(pad, oc)
                info["facing"] = {
                    "target": other.name,
                    "edge_mm": list(esc["edge"]),
                    "escape_mm": list(esc["escape"]),
                    "unit": list(esc["unit"]),
                    "compass": list(esc["compass"]) if esc["compass"] else None,
                }
            except LayFailure as error:
                info["facing_error"] = error.reason
        out.append(info)
    return {"ok": True, "verb": "pads", "status": "ok", "pads": out}


def execute_zones(board) -> dict:
    """Read-only inspection of enabled layers, Edge.Cuts outline, copper pours and rule areas."""
    return {
        "ok": True,
        "verb": "zones",
        "status": "ok",
        "enabled_layers": list(board.enabled_layers()),
        "board_outline": board.board_outline(),
        "zones": board.zones(),
    }


def execute_delete_zones(board, zone_ids: list[str], *, run_drc: bool = True) -> dict:
    """Delete unlocked copper zones by ID (refusing rule areas), refill remaining zones and verify DRC."""
    began = time.time()
    clean_ids = list(dict.fromkeys(str(z).strip() for z in zone_ids if str(z).strip()))
    if not clean_ids:
        raise LayFailure("bad_request", "delete-zones requires at least one zone ID")
    existing = {z["id"]: z for z in board.zones()}
    for zid in clean_ids:
        z = existing.get(zid)
        if z is None:
            raise LayFailure("not_found", f"zone {zid} not found on board")
        if z["type"] == "rule_area":
            raise LayFailure("forbidden", f"zone {zid} is a rule area; rule areas cannot be deleted")
        if z.get("locked"):
            raise LayFailure("locked", f"zone {zid} is locked and cannot be deleted")
    info = board.delete_zones(clean_ids, f"Delete {len(clean_ids)} copper zone(s) via IPC", run_drc=run_drc)
    return {
        "ok": True,
        "verb": "delete-zones",
        "status": "deleted",
        "zones_deleted": len(info["deleted_ids"]),
        "deleted_ids": info["deleted_ids"],
        "drc": info["drc"],
        "seconds": round(time.time() - began, 2),
    }


def _batch_label(plans: list, pours: list, standalone_vias: list) -> str:
    parts = []
    if plans:
        parts.append(f"{len(plans)} route(s)")
    if pours:
        parts.append(f"{len(pours)} pour(s)")
    if standalone_vias:
        parts.append(f"{len(standalone_vias)} via(s)")
    return f"Lay {', '.join(parts)} via IPC"


def _required_clearance_mm(
    board, net_a: str, net_b: str, dru_floor: float | None, nc_cache: dict[str, float]
) -> float:
    vals: list[float] = []
    for net in (net_a, net_b):
        if not net:
            continue
        if net not in nc_cache:
            nc = board.netclass_defaults(net)
            raw = nc.get("clearance_mm") if isinstance(nc, dict) else None
            nc_cache[net] = float(raw) if raw is not None and float(raw) > 0 else 0.0
        if nc_cache[net] > 0:
            vals.append(nc_cache[net])
    nc_req = max(vals) if vals else 0.0
    if dru_floor is not None and nc_req > 0.0:
        return min(nc_req, dru_floor)
    return nc_req


def _seg_bbox(a: tuple[float, float], b: tuple[float, float], rad: float) -> tuple[float, float, float, float]:
    return (
        min(a[0], b[0]) - rad,
        min(a[1], b[1]) - rad,
        max(a[0], b[0]) + rad,
        max(a[1], b[1]) + rad,
    )


def _boxes_close(a: tuple[float, float, float, float], b: tuple[float, float, float, float], margin: float) -> bool:
    return not (a[2] + margin < b[0] or b[2] + margin < a[0] or a[3] + margin < b[1] or b[3] + margin < a[1])


def _check_planned_obstacles(
    all_segments: list[PlannedSegment],
    all_vias: list[PlannedVia],
    existing_items: list[kr.Item],
    pad_obstacles: list[dict],
    board,
) -> None:
    """Fast 2D collision and clearance pre-check against live items, pads, and intra-batch copper."""
    dru_floor = board.min_clearance_floor_mm() if hasattr(board, "min_clearance_floor_mm") else None
    nc_cache: dict[str, float] = {}
    for seg in all_segments:
        if seg.net not in nc_cache:
            _required_clearance_mm(board, seg.net, "", dru_floor, nc_cache)
    for via in all_vias:
        if via.net not in nc_cache:
            _required_clearance_mm(board, via.net, "", dru_floor, nc_cache)
    coarse_margin = max(2.0, max(nc_cache.values(), default=0.0)) + 0.05

    msgs: list[str] = []
    collisions: list[dict] = []

    def record_if_bad(p_label: str, p_net: str, o_label: str, o_net: str, gap: float) -> None:
        req = _required_clearance_mm(board, p_net, o_net, dru_floor, nc_cache)
        tol = 0.002 if req > 0.0 else -0.001
        if gap < req - tol:
            verb = "hits" if gap <= 0.001 else "violates clearance with"
            msgs.append(f"{p_label} {verb} {o_label} (gap={gap:.3f} mm, need {req:.3f} mm)")
            collisions.append({
                "planned": p_label,
                "obstacle": o_label,
                "net": p_net,
                "obstacle_net": o_net,
                "gap_mm": round(gap, 4),
                "required_mm": round(req, 4),
            })

    existing_boxes = [
        _seg_bbox(
            item.start,
            item.start if item.kind == "via" else item.end,
            item.width / 2.0,
        )
        for item in existing_items
    ]
    pad_boxes = [
        (
            pad["center"][0] - math.hypot(pad["size"][0], pad["size"][1]) / 2.0,
            pad["center"][1] - math.hypot(pad["size"][0], pad["size"][1]) / 2.0,
            pad["center"][0] + math.hypot(pad["size"][0], pad["size"][1]) / 2.0,
            pad["center"][1] + math.hypot(pad["size"][0], pad["size"][1]) / 2.0,
        )
        for pad in pad_obstacles
    ]
    seg_boxes = [_seg_bbox(seg.start, seg.end, seg.width / 2.0) for seg in all_segments]
    via_boxes = [_seg_bbox(via.at, via.at, via.diameter / 2.0) for via in all_vias]

    def fmt_seg(prefix: str, seg: PlannedSegment) -> str:
        return (
            f"{prefix} {seg.net} ({_fmt_num(seg.start[0])}, {_fmt_num(seg.start[1])})"
            f"->({_fmt_num(seg.end[0])}, {_fmt_num(seg.end[1])}) on {seg.layer}"
        )

    def fmt_via(prefix: str, net: str, at: tuple[float, float], dia: float) -> str:
        return f"{prefix} {net} via at ({_fmt_num(at[0])}, {_fmt_num(at[1])}) d={_fmt_num(dia)} mm"

    for idx, seg in enumerate(all_segments):
        if len(msgs) >= 5:
            break
        p_box = seg_boxes[idx]
        p_label = fmt_seg("planned", seg)
        p_rad = seg.width / 2.0
        for item, o_box in zip(existing_items, existing_boxes):
            if item.net == seg.net or not _boxes_close(p_box, o_box, coarse_margin):
                continue
            obs_net = item.net or "<unconnected>"
            if item.kind == "via":
                gap = _dist_point_to_seg(item.start[0], item.start[1], seg.start[0], seg.start[1], seg.end[0], seg.end[1]) - p_rad - item.width / 2.0
                record_if_bad(p_label, seg.net, fmt_via("existing", obs_net, item.start, item.width), item.net, gap)
            elif item.kind == "track" and item.layer == seg.layer:
                gap = _dist_seg_to_seg(seg.start, seg.end, item.start, item.end) - p_rad - item.width / 2.0
                o_label = (
                    f"existing {obs_net} {item.layer} track ({_fmt_num(item.start[0])}, {_fmt_num(item.start[1])})"
                    f"->({_fmt_num(item.end[0])}, {_fmt_num(item.end[1])}) w={_fmt_num(item.width)} mm"
                )
                record_if_bad(p_label, seg.net, o_label, item.net, gap)
            if len(msgs) >= 5:
                break
        if len(msgs) >= 5:
            break
        for pad, o_box in zip(pad_obstacles, pad_boxes):
            if pad["net"] == seg.net or seg.layer not in pad["layers"] or not _boxes_close(p_box, o_box, coarse_margin):
                continue
            obs_net = pad["net"] or "<unconnected>"
            gap = _dist_to_pad("track", seg.start, seg.end, pad) - p_rad
            o_label = f"pad {pad['name']} ({obs_net}) at ({_fmt_num(pad['center'][0])}, {_fmt_num(pad['center'][1])})"
            record_if_bad(p_label, seg.net, o_label, pad["net"], gap)
            if len(msgs) >= 5:
                break
        if len(msgs) >= 5:
            break
        for j in range(idx + 1, len(all_segments)):
            other = all_segments[j]
            if other.net == seg.net or other.layer != seg.layer or not _boxes_close(p_box, seg_boxes[j], coarse_margin):
                continue
            gap = _dist_seg_to_seg(seg.start, seg.end, other.start, other.end) - p_rad - other.width / 2.0
            record_if_bad(p_label, seg.net, fmt_seg("batch", other), other.net, gap)
            if len(msgs) >= 5:
                break
        if len(msgs) >= 5:
            break
        for via, o_box in zip(all_vias, via_boxes):
            if via.net == seg.net or not _boxes_close(p_box, o_box, coarse_margin):
                continue
            gap = _dist_point_to_seg(via.at[0], via.at[1], seg.start[0], seg.start[1], seg.end[0], seg.end[1]) - p_rad - via.diameter / 2.0
            record_if_bad(p_label, seg.net, fmt_via("batch", via.net, via.at, via.diameter), via.net, gap)
            if len(msgs) >= 5:
                break

    for idx, via in enumerate(all_vias):
        if len(msgs) >= 5:
            break
        p_box = via_boxes[idx]
        p_label = fmt_via("planned", via.net, via.at, via.diameter)
        p_rad = via.diameter / 2.0
        for item, o_box in zip(existing_items, existing_boxes):
            if item.net == via.net or not _boxes_close(p_box, o_box, coarse_margin):
                continue
            obs_net = item.net or "<unconnected>"
            if item.kind == "via":
                gap = math.hypot(via.at[0] - item.start[0], via.at[1] - item.start[1]) - p_rad - item.width / 2.0
                record_if_bad(p_label, via.net, fmt_via("existing", obs_net, item.start, item.width), item.net, gap)
            elif item.kind == "track":
                gap = _dist_point_to_seg(via.at[0], via.at[1], item.start[0], item.start[1], item.end[0], item.end[1]) - p_rad - item.width / 2.0
                o_label = (
                    f"existing {obs_net} {item.layer} track ({_fmt_num(item.start[0])}, {_fmt_num(item.start[1])})"
                    f"->({_fmt_num(item.end[0])}, {_fmt_num(item.end[1])}) w={_fmt_num(item.width)} mm"
                )
                record_if_bad(p_label, via.net, o_label, item.net, gap)
            if len(msgs) >= 5:
                break
        if len(msgs) >= 5:
            break
        for pad, o_box in zip(pad_obstacles, pad_boxes):
            if pad["net"] == via.net or not pad["layers"] or not _boxes_close(p_box, o_box, coarse_margin):
                continue
            obs_net = pad["net"] or "<unconnected>"
            gap = _dist_to_pad("via", via.at, via.at, pad) - p_rad
            o_label = f"pad {pad['name']} ({obs_net}) at ({_fmt_num(pad['center'][0])}, {_fmt_num(pad['center'][1])})"
            record_if_bad(p_label, via.net, o_label, pad["net"], gap)
            if len(msgs) >= 5:
                break
        if len(msgs) >= 5:
            break
        for j in range(idx + 1, len(all_vias)):
            other = all_vias[j]
            if other.net == via.net or not _boxes_close(p_box, via_boxes[j], coarse_margin):
                continue
            gap = math.hypot(via.at[0] - other.at[0], via.at[1] - other.at[1]) - p_rad - other.diameter / 2.0
            record_if_bad(p_label, via.net, fmt_via("batch", other.net, other.at, other.diameter), other.net, gap)
            if len(msgs) >= 5:
                break

    if msgs:
        raise LayFailure("obstacle_collision", "; ".join(msgs), collisions=collisions)


def _point_hits_copper(pt: tuple[float, float], layer: str, net: str, items: list[kr.Item]) -> bool:
    """Return True if *pt* lands on an existing same-net track on *layer* or via."""
    for item in items:
        if item.net != net:
            continue
        if item.kind == "via":
            if math.hypot(item.start[0] - pt[0], item.start[1] - pt[1]) <= max(item.width / 2.0, 0.005):
                return True
        elif item.kind == "track" and item.layer == layer:
            if kr._near_segment(pt, item.start, item.end, max(item.width / 2.0, 0.005)):
                return True
    return False


def execute_batch(board, spec: dict, *, run_drc: bool = True) -> dict:
    """Validate, atomically create/refill, verify and optionally roll back routes, pours and standalone vias."""
    began = time.time()
    if hasattr(board, "invalidate_copper_cache"):
        board.invalidate_copper_cache()
    raw_routes = spec.get("routes") or []
    raw_pours = spec.get("pours") or []
    raw_vias = spec.get("vias") or []
    if not isinstance(raw_routes, list) or not isinstance(raw_pours, list) or not isinstance(raw_vias, list):
        raise LayFailure("bad_request", "'routes', 'pours' and 'vias' must be lists")
    if not raw_routes and not raw_pours and not raw_vias:
        raise LayFailure("bad_request", "batch requires at least one non-empty 'routes', 'pours' or 'vias' list")

    defaults = {k: spec[k] for k in ("layer", "layers", "width", "via_diameter", "via_drill",
                                      "centered_exits", "escape_mm", "net", "clearance",
                                      "min_thickness", "connect", "thermal_gap", "thermal_bridge_width",
                                      "island_mode", "min_island_area", "inset", "priority", "replace")
                if k in spec and spec[k] is not None}

    plans = [plan_route(route_spec, board, defaults) for route_spec in raw_routes]
    pours = [plan_pour(pour_spec, board, defaults) for pour_spec in raw_pours]
    standalone_vias = [plan_via(via_spec, board, defaults) for via_spec in raw_vias]
    all_segments = [seg for plan in plans for seg in plan["segments"]]
    all_vias = [via for plan in plans for via in plan["vias"]] + standalone_vias
    replace_ids = list(dict.fromkeys(zid for pour in pours for zid in pour.replace_ids))
    if not all_segments and not all_vias and not pours:
        raise LayFailure("bad_request", "planned batch produced no segments, vias or pours")

    has_to_point = any(p.get("kind") == "to_point" for p in plans)
    existing_items = board.items() if (all_segments or all_vias) else []
    if has_to_point:
        plan_items = [
            [
                kr.Item(f"plan-{i}-s-{j}", "track", seg.net, seg.layer, seg.start, seg.end, seg.width)
                for j, seg in enumerate(p["segments"])
            ] + [
                kr.Item(f"plan-{i}-v-{j}", "via", via.net, None, via.at, via.at, via.diameter)
                for j, via in enumerate(p["vias"])
            ]
            for i, p in enumerate(plans)
        ]
        standalone_via_items = [
            kr.Item(f"standalone-v-{j}", "via", via.net, None, via.at, via.at, via.diameter)
            for j, via in enumerate(standalone_vias)
        ]
        for idx, p in enumerate(plans):
            if p.get("kind") != "to_point":
                continue
            candidates = list(existing_items) + standalone_via_items
            for other_idx, items in enumerate(plan_items):
                if other_idx != idx:
                    candidates.extend(items)
            if not _point_hits_copper(p["to_point"], p["end_layer"], p["net"], candidates):
                raise LayFailure(
                    "no_target_copper",
                    f"to_point {list(p['to_point'])} on {p['end_layer']} ({p['net']}) does not land on any existing {p['net']} track or via",
                    net=p["net"],
                    to_point=list(p["to_point"]),
                    layer=p["end_layer"],
                )
    if all_segments or all_vias:
        pad_obs = board.pad_obstacles() if hasattr(board, "pad_obstacles") else []
        _check_planned_obstacles(all_segments, all_vias, existing_items, pad_obs, board)

    has_to_via = any(p.get("kind") == "to_via" for p in plans)
    label = _batch_label(plans, pours, standalone_vias)
    kwargs: dict = {}
    if pours or replace_ids:
        kwargs["pours"] = pours
        kwargs["replace_zone_ids"] = replace_ids
    if has_to_via:
        kwargs["refill_zones"] = True
    tx = board.create_copper(all_segments, all_vias, label, run_drc=run_drc, **kwargs)
    to_via_pour_layers: dict[int, list[str]] = {}
    try:
        live_items = board.items()
        live_ids = {item.id for item in live_items}
        if not tx["created_ids"].issubset(live_ids):
            raise LayFailure("read_back_differs", "created copper items missing on read-back")
        if all_segments or all_vias:
            created_live = [item for item in live_items if item.id in tx["created_ids"]]
            live_segs = [item for item in created_live if item.kind == "track"]
            live_vias = [item for item in created_live if item.kind == "via"]
            if len(live_segs) != len(all_segments) or len(live_vias) != len(all_vias):
                raise LayFailure(
                    "read_back_differs",
                    f"expected {len(all_segments)} track(s) and {len(all_vias)} via(s) on read-back, "
                    f"got {len(live_segs)} track(s) and {len(live_vias)} via(s)",
                )
        check_vias = list(standalone_vias) + [p["vias"][-1] for p in plans if p.get("kind") == "to_via" and p.get("vias")]
        if check_vias:
            created_vias = [item for item in live_items if item.id in tx["created_ids"] and item.kind == "via"]
            for via in check_vias:
                matches = [item for item in created_vias if same_pt(item.start, via.at)]
                if not matches or not any(item.net == via.net for item in matches):
                    got = matches[0].net if matches else None
                    raise LayFailure(
                        "read_back_differs",
                        f"created {via.net} via at {list(via.at)} changed net on read-back (got {got!r})",
                        net=via.net,
                        actual_net=got,
                    )

        live_zones = board.zones() if (pours or replace_ids) else []
        created_zone_ids = set(tx.get("created_zone_ids") or ())
        if pours:
            by_id = {z["id"]: z for z in live_zones}
            if created_zone_ids and not created_zone_ids.issubset(by_id):
                raise LayFailure("read_back_differs", "created copper zone missing on read-back")
            created_zones = [by_id[zid] for zid in created_zone_ids] if created_zone_ids else live_zones
            for pour in pours:
                matched = [
                    z for z in created_zones
                    if z["type"] == "copper" and z["net"] == pour.net and set(pour.layers).issubset(z["layers"])
                ]
                filled_layers = {layer for z in matched for layer in z.get("filled_layers", [])}
                if not matched or not set(pour.layers).issubset(filled_layers):
                    missing = sorted(set(pour.layers) - filled_layers)
                    raise LayFailure(
                        "empty_pour",
                        f"pour on {pour.net} ({','.join(missing or pour.layers)}) produced no filled copper",
                        net=pour.net,
                        layers=list(pour.layers),
                    )

        after_q = board.quality()
        new_errors = [msg for key, msg in after_q["errors"].items() if key not in tx["before_quality"]["errors"]]
        if new_errors:
            raise LayFailure("route_quality", f"route quality rejected new error(s): {'; '.join(new_errors[:5])}",
                             findings=new_errors)

        enabled_layers = tuple(board.enabled_layers()) if has_to_via else ()
        unclosed = []
        for p in plans:
            if p.get("kind") == "to_via":
                v_pt = p["to_via"]
                via_pad = kr.PadInfo(
                    name=p["to"], net=p["net"], x=v_pt[0], y=v_pt[1],
                    layers=enabled_layers, half=p["via_diameter"] / 2.0, through=True, round=True,
                )
                same_spot = same_pt((p["pad_a"].x, p["pad_a"].y), v_pt)
                via_present = any(item.kind == "via" and item.net == p["net"] and same_pt(item.start, v_pt) for item in live_items)
                is_conn = (same_spot and via_present) or kr.connected(p["pad_a"], via_pad, live_items)
            elif p.get("kind") == "to_point":
                t_pt = p["to_point"]
                pt_pad = kr.PadInfo(
                    name=p["to"], net=p["net"], x=t_pt[0], y=t_pt[1],
                    layers=(p["end_layer"],), half=max(p["end_width"] / 2.0, 0.005), through=False, round=True,
                )
                same_spot = same_pt((p["pad_a"].x, p["pad_a"].y), t_pt)
                is_conn = (same_spot and p["end_layer"] in p["pad_a"].layers) or kr.connected(p["pad_a"], pt_pad, live_items)
            else:
                is_conn = kr.connected(p["pad_a"], p["pad_b"], live_items)
            if not is_conn:
                unclosed.append(f"{p['from']}->{p['to']} ({p['net']})")
        if unclosed:
            raise LayFailure("not_connected", f"planned copper did not close connection(s): {', '.join(unclosed)}",
                             open_connections=unclosed)

        if has_to_via:
            filled_cache: dict[tuple[str, str | None], dict] = {}
            for idx, p in enumerate(plans):
                if p.get("kind") != "to_via":
                    continue
                net_key = (p["net"], p.get("pour_layer"))
                if net_key not in filled_cache:
                    layer_filter = (p["pour_layer"],) if p.get("pour_layer") else None
                    filled_cache[net_key] = board.filled_zone_polygons(p["net"], layer_filter)
                by_layer = filled_cache[net_key]
                matched_layers = sorted(
                    layer for layer, polys in by_layer.items()
                    if point_in_filled_zone(p["to_via"], polys)
                )
                if not matched_layers:
                    target_hint = f" on {p['pour_layer']}" if p.get("pour_layer") else ""
                    raise LayFailure(
                        "not_in_pour",
                        f"via at {list(p['to_via'])} for {p['from']} ({p['net']}) is not inside a filled {p['net']} copper pour{target_hint}",
                        net=p["net"],
                        to_via=list(p["to_via"]),
                        pour_layer=p.get("pour_layer"),
                    )
                to_via_pour_layers[idx] = matched_layers

        drc_info = {"skipped": True}
        if run_drc:
            drc_info = board.run_drc_diff(tx["before_text"], board.text())
            if drc_info["new_errors"]:
                first = drc_info["new_errors"][0]
                raise LayFailure("drc_failed", f"new DRC error: {first.get('description')}",
                                 violations=drc_info["new_errors"], drc=drc_info)
            unconn = drc_info.get("unconnected")
            if isinstance(unconn, (list, tuple)) and len(unconn) == 2 and unconn[1] > unconn[0] and not replace_ids:
                raise LayFailure(
                    "drc_failed",
                    f"unconnected items increased from {unconn[0]} to {unconn[1]}",
                    drc=drc_info,
                )

        board.finalize_commit(tx, label)
    except Exception:
        board.rollback_commit(tx, label)
        raise

    new_warnings = [msg for key, msg in after_q["warnings"].items() if key not in tx["before_quality"]["warnings"]]
    verb = "batch" if (len(plans) + len(pours) + (1 if standalone_vias else 0) > 1 or standalone_vias) else ("pour" if pours else "route")
    pour_summaries = []
    if pours:
        target_zones = [z for z in live_zones if not created_zone_ids or z["id"] in created_zone_ids]
        for pour in pours:
            matching = [z for z in target_zones if z["type"] == "copper" and z["net"] == pour.net and set(pour.layers).issubset(z["layers"])]
            pour_summaries.append({
                "net": pour.net,
                "layers": list(pour.layers),
                "name": pour.name,
                "zone_ids": [z["id"] for z in matching],
                "filled_layers": sorted({layer for z in matching for layer in z.get("filled_layers", [])}),
            })
    route_summaries = []
    for idx, p in enumerate(plans):
        entry = {
            "from": p["from"],
            "to": p["to"],
            "net": p["net"],
            "segments": len(p["segments"]),
            "vias": len(p["vias"]),
        }
        if p.get("kind") == "to_via":
            entry["to_via"] = list(p["to_via"])
            entry["pour_layers"] = to_via_pour_layers.get(idx, [])
        elif p.get("kind") == "to_point":
            entry["to_point"] = list(p["to_point"])
            entry["end_layer"] = p["end_layer"]
        route_summaries.append(entry)
    return {
        "ok": True,
        "verb": verb,
        "status": "laid",
        "tracks_created": len(all_segments),
        "vias_created": len(all_vias),
        "zones_created": len(pours),
        "zones_replaced": len(replace_ids),
        "routes": route_summaries,
        **({"pours": pour_summaries} if pours else {}),
        "quality_warnings": new_warnings,
        "drc": drc_info,
        "seconds": round(time.time() - began, 2),
    }


class _OrderedStepAction(argparse.Action):
    def __init__(self, option_strings, dest, step_kind, **kwargs):
        self.step_kind = step_kind
        super().__init__(option_strings, dest, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        items = getattr(namespace, self.dest, None)
        if items is None:
            items = []
        items.append({self.step_kind: list(kr.point_arg(values))})
        setattr(namespace, self.dest, items)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Lay checked atomic copper in the open PCB Editor over IPC.")
    parser.add_argument("--pid", help="the PCB Editor pid to use when several are open")
    verbs = parser.add_subparsers(dest="verb", required=True)

    p_pads = verbs.add_parser("pads", help="inspect pad centers, shapes, layers, netclasses and facing short edges")
    p_pads.add_argument("pads", nargs="+")
    p_pads.add_argument("--facing", help="reference target pad to compute facing short-edge escape vector toward")

    p_route = verbs.add_parser("route", help="lay one checked connection over IPC")
    p_route.add_argument("start", help="start pad, like J1.1")
    p_route.add_argument("target", help="target pad, like J2.1")
    p_route.add_argument("--net", help="expected net name")
    p_route.add_argument("--layer", help="starting copper layer (e.g. F.Cu)")
    p_route.add_argument("--width", type=kr.width_arg, help="track width in mm")
    p_route.add_argument("--via-dia", type=kr.width_arg, dest="via_diameter", help="via diameter in mm")
    p_route.add_argument("--via-drill", type=kr.width_arg, help="via drill diameter in mm")
    p_route.add_argument("--through", dest="steps", action=_OrderedStepAction, step_kind="through", metavar="X,Y",
                         help="intermediate corner coordinate in mm (repeatable in order)")
    p_route.add_argument("--via", dest="steps", action=_OrderedStepAction, step_kind="via", metavar="X,Y",
                         help="via coordinate in mm (repeatable in order)")
    p_route.add_argument("--via-layer", action="append", default=[], help="layer after the N-th --via (defaults to other outer layer)")
    p_route.add_argument("--centered-exits", action="store_true", help="enforce perpendicular short-edge exits at both pads")
    p_route.add_argument("--escape-mm", type=float, default=DEFAULT_ESCAPE_MM, help="straight stub length past short edge")
    p_route.add_argument("--no-drc", action="store_true", help="skip kicad-cli DRC verification (not recommended)")

    p_batch = verbs.add_parser("batch", help="lay routes, pours and stitching vias atomically from a JSON spec")
    src = p_batch.add_mutually_exclusive_group(required=True)
    src.add_argument("--json", help="inline JSON batch spec (preferred for routing agents; never write scratch files)")
    src.add_argument("--stdin", action="store_true", help="read JSON batch spec from stdin via heredoc (preferred for multi-wire batches)")
    src.add_argument("--file", type=pathlib.Path, help="path to a pre-existing local JSON batch spec file (manual CLI use only; agents must use --json or --stdin)")
    p_batch.add_argument("--no-drc", action="store_true", help="skip kicad-cli DRC verification (not recommended)")

    verbs.add_parser("zones", help="inspect board outline, enabled copper layers, pours and rule areas")

    p_pour = verbs.add_parser("pour", help="create or replace a checked copper pour zone over IPC")
    p_pour.add_argument("--net", required=True, help="exact net name on the board (e.g. GND)")
    p_pour.add_argument("--layer", help="single copper layer (e.g. In1.Cu)")
    p_pour.add_argument("--layers", help="comma-separated copper layers (e.g. F.Cu,B.Cu)")
    outline_group = p_pour.add_mutually_exclusive_group(required=True)
    outline_group.add_argument("--board-outline", action="store_true", help="follow the board's Edge.Cuts outline")
    outline_group.add_argument("--rect", nargs=4, type=float, metavar=("X0", "Y0", "X1", "Y1"), help="rectangle outline in mm")
    outline_group.add_argument("--polygon", help="JSON list of [x, y] vertices in mm")
    p_pour.add_argument("--inset", type=float, default=0.0, help="inward setback from the chosen outline in mm")
    p_pour.add_argument("--holes", help="JSON list of hole polygons [[[x, y], ...]] in mm")
    p_pour.add_argument("--name", default="", help="zone name")
    p_pour.add_argument("--priority", type=int, default=0, help="zone fill priority (non-negative int)")
    p_pour.add_argument("--clearance", type=float, help="local copper clearance in mm")
    p_pour.add_argument("--min-thickness", type=float, default=DEFAULT_ZONE_MIN_THICKNESS_MM, help="minimum copper bridge width in mm")
    p_pour.add_argument("--connect", choices=ZONE_CONNECT_MODES, default="thermal", help="pad connection style")
    p_pour.add_argument("--thermal-gap", type=float, help="thermal relief gap in mm")
    p_pour.add_argument("--thermal-bridge", type=float, help="thermal spoke width in mm")
    p_pour.add_argument("--island-mode", choices=ZONE_ISLAND_MODES, default="always", help="copper island removal mode")
    p_pour.add_argument("--min-island-area", type=float, default=DEFAULT_ZONE_MIN_ISLAND_MM2, help="minimum island area in mm^2")
    p_pour.add_argument("--replace", action="store_true", help="replace existing unlocked copper zones on the same net and layer set")
    p_pour.add_argument("--no-drc", action="store_true", help="skip kicad-cli DRC verification (not recommended)")

    p_del_z = verbs.add_parser("delete-zones", help="delete unlocked copper zones by id (never rule areas)")
    p_del_z.add_argument("ids", nargs="+", help="copper zone UUIDs to remove")
    p_del_z.add_argument("--no-drc", action="store_true", help="skip kicad-cli DRC verification (not recommended)")
    return parser.parse_args(argv)


def _pour_spec_from_args(args: argparse.Namespace) -> dict:
    spec: dict = {
        "net": args.net,
        "inset": args.inset,
        "name": args.name,
        "priority": args.priority,
        "min_thickness": args.min_thickness,
        "connect": args.connect,
        "island_mode": args.island_mode,
        "min_island_area": args.min_island_area,
        "replace": bool(args.replace),
    }
    if args.layer is not None and args.layers is not None:
        raise LayFailure("bad_request", "pass either --layer or --layers, not both")
    if args.layers is not None:
        spec["layers"] = args.layers
    elif args.layer is not None:
        spec["layer"] = args.layer
    if args.board_outline:
        spec["outline"] = "board"
    elif args.rect is not None:
        spec["rect"] = list(args.rect)
    elif args.polygon is not None:
        try:
            spec["polygon"] = json.loads(args.polygon)
        except ValueError as error:
            raise LayFailure("bad_request", f"invalid --polygon JSON: {error}") from None
    if args.holes is not None:
        try:
            spec["holes"] = json.loads(args.holes)
        except ValueError as error:
            raise LayFailure("bad_request", f"invalid --holes JSON: {error}") from None
    if args.clearance is not None:
        spec["clearance"] = args.clearance
    if args.thermal_gap is not None:
        spec["thermal_gap"] = args.thermal_gap
    if args.thermal_bridge is not None:
        spec["thermal_bridge_width"] = args.thermal_bridge
    return {"pours": [spec]}


def _route_spec_from_args(args: argparse.Namespace) -> dict:
    steps = list(args.steps or [])
    via_layers = list(args.via_layer or [])
    via_idx = 0
    for step in steps:
        if "via" in step:
            if via_idx < len(via_layers):
                step["layer"] = via_layers[via_idx]
            via_idx += 1
    return {
        "routes": [{
            "from": args.start,
            "to": args.target,
            **({"net": args.net} if args.net else {}),
            **({"layer": args.layer} if args.layer else {}),
            **({"width": args.width} if args.width is not None else {}),
            **({"via_diameter": args.via_diameter} if args.via_diameter is not None else {}),
            **({"via_drill": args.via_drill} if args.via_drill is not None else {}),
            "centered_exits": bool(args.centered_exits),
            "escape_mm": args.escape_mm,
            "steps": steps,
        }],
    }


def _batch_spec_from_args(args: argparse.Namespace) -> dict:
    if args.file is not None:
        raw = args.file.read_text(encoding="utf-8")
    elif args.json is not None:
        raw = args.json
    else:
        raw = sys.stdin.read()
    try:
        spec = json.loads(raw)
    except ValueError as error:
        raise LayFailure("bad_request", f"invalid JSON spec: {error}") from None
    if not isinstance(spec, dict):
        raise LayFailure("bad_request", "batch JSON must be an object")
    return spec


def main(argv: list[str] | None = None, board=None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        spec = None
        if args.verb == "route":
            spec = _route_spec_from_args(args)
            _prevalidate_batch_spec(spec)
        elif args.verb == "pour":
            spec = _pour_spec_from_args(args)
            _prevalidate_batch_spec(spec)
        elif args.verb == "batch":
            spec = _batch_spec_from_args(args)
            _prevalidate_batch_spec(spec)

        if board is None and not mac.LOCAL:
            raise LayFailure("bad_request", "run this on the Mac with .venv-kicad-ipc/bin/python where the editor is open")
        board = board or KipyLayBoard(args.pid)
        if args.verb == "pads":
            result = execute_pads(board, args.pads, args.facing)
        elif args.verb == "zones":
            result = execute_zones(board)
        elif args.verb == "route":
            result = execute_batch(board, spec, run_drc=not args.no_drc)
            result["verb"] = "route"
        elif args.verb == "pour":
            result = execute_batch(board, spec, run_drc=not args.no_drc)
            result["verb"] = "pour"
        elif args.verb == "delete-zones":
            result = execute_delete_zones(board, args.ids, run_drc=not args.no_drc)
        else:
            result = execute_batch(board, spec, run_drc=not args.no_drc)
            result["verb"] = "batch"
    except kr.RouteFailure as failure:
        result = {"ok": False, "verb": args.verb, "status": failure.status, "reason": failure.reason, **failure.extra}
    except LayFailure as failure:
        result = {"ok": False, "verb": args.verb, "status": failure.status, "reason": failure.reason, **failure.extra}
    except mac.MacError as error:
        result = {"ok": False, "verb": args.verb, "status": "mac_failed", "reason": str(error)}
    except Exception as error:
        result = {"ok": False, "verb": args.verb, "status": "operation_failed", "reason": f"{type(error).__name__}: {error}"}
    if board is not None and "board" not in result:
        try:
            result["board"] = board.file()
        except Exception:
            pass
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        mac.report_routing_handoff()
