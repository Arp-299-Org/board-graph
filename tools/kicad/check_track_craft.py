#!/usr/bin/env python3
"""Measure track craft on a KiCad board, independent of DRC.

Board track-craft rules:
  via-to-pad gap >= 0.2 mm (JLCPCB minimum solder-mask dam)
  * a single track that turns does so at 45 degrees, never 90; a "chamfer"
    shorter than the track width is a corner, not a chamfer;
  * branches (three or more segments at a point) may leave at 90 or 45;
  * avoid sharp notches in the copper outline; an acute centreline angle is
    only a candidate for inspection, not proof of a notch;
  * a track ends on its pad centre, enters along one of the pad's axes, and
    its end cap stays inside the pad outline;
  * a via never sits inside, touching, or within 0.2 mm (edge to edge) of a same-net pad;
  * tracks into 0402 pads neck to 0.25 mm for at least 0.3 mm beyond the pad
    edge, including end caps, via rings and teardrop copper; narrow pins have no exact
    0.15 mm neck requirement, and a short narrower escape may be needed;
  * the bend before a pad sits outside the pad outline;
  * no bend within a via's reach that is not on the via itself.
Corners covered by a same-net pad or via ring are reported separately. An
inspection candidate is not a verified defect and prevents an automatic pass.

Runs under KiCad's Python, through the guarded runner (Windows or macOS):
    ./scripts/kicad_run.ps1 -Mode Offline -Project board -Script tools/kicad/check_track_craft.py -ScriptArgs @('board.kicad_pcb')
Exit status 1 when any rule fails.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import pathlib
import re
import sys
from collections import defaultdict

import pcbnew

mm = pcbnew.ToMM
CLR = 0.2


def footprint_is_inch_0402(identifier):
    """Recognize a 0402-inch package token, not e.g. 01005_0402Metric."""
    return bool(re.search(r"(?:^|[_:/-])[CR]?0402(?:[_-]|$)", identifier, re.IGNORECASE))


def pad_boundary_depth(pad, layer, x, y):
    """Signed distance inside standard pad copper, in mm; None for other shapes.

    Rectangles, circles, ovals and rounded rectangles use the pad's own size,
    shape position and rotation. The rounded-rectangle signed-distance formula
    measures its real perimeter, not its bounding box or internal primitives.
    """
    shape = pad.GetShape(layer)
    supported = (pcbnew.PAD_SHAPE_RECTANGLE, pcbnew.PAD_SHAPE_CIRCLE,
                 pcbnew.PAD_SHAPE_OVAL, pcbnew.PAD_SHAPE_ROUNDRECT)
    if shape not in supported:
        return None
    origin, size = pad.ShapePos(layer), pad.GetSize(layer)
    hx, hy = mm(size.x) / 2, mm(size.y) / 2
    if hx <= 0 or hy <= 0:
        return None
    if shape == pcbnew.PAD_SHAPE_RECTANGLE:
        radius = 0.0
    elif shape == pcbnew.PAD_SHAPE_ROUNDRECT:
        radius = 2 * min(hx, hy) * pad.GetRoundRectRadiusRatio(layer)
    else:
        radius = min(hx, hy)
    angle = math.radians(pad.GetOrientationDegrees())
    dx, dy = x - mm(origin.x), y - mm(origin.y)
    local_x = dx * math.cos(angle) - dy * math.sin(angle)
    local_y = dx * math.sin(angle) + dy * math.cos(angle)
    qx, qy = abs(local_x) - (hx - radius), abs(local_y) - (hy - radius)
    signed = math.hypot(max(qx, 0), max(qy, 0)) + min(max(qx, qy), 0) - radius
    return -signed


def pad_exit_distance(pad, layer, x, y, ux, uy):
    """Distance from an interior point to its actual pad boundary along a ray."""
    depth = pad_boundary_depth(pad, layer, x, y)
    if depth is None or depth < 0:
        return None
    size = pad.GetSize(layer)
    lo, hi = 0.0, math.hypot(mm(size.x), mm(size.y)) + 1
    for _ in range(45):
        mid = (lo + hi) / 2
        if pad_boundary_depth(pad, layer, x + ux * mid, y + uy * mid) >= 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def pad_via_gap(pad, via, layer):
    """Edge gap from KiCad's effective pad copper to the circular via ring."""
    if not pad.IsOnLayer(layer) or not via.IsOnLayer(layer):
        return None
    center = via.GetPosition()
    distance = mm(pad.GetEffectiveShape(layer).Distance(center))
    return max(0.0, distance - mm(via.GetWidth(layer)) / 2)


def load(path):
    target = pathlib.Path(path)
    if not target.is_file():
        raise FileNotFoundError(f"Board file does not exist: {path}")
    return pcbnew.LoadBoard(str(target))


def analyse(board) -> dict:
    pads = []
    for fp in board.GetFootprints():
        for p in fp.Pads():
            bb = p.GetBoundingBox()
            l, t, r, b = mm(bb.GetLeft()), mm(bb.GetTop()), mm(bb.GetRight()), mm(bb.GetBottom())
            pads.append({"net": p.GetNetname(), "l": l, "t": t, "r": r, "b": b, "cx": mm(p.GetPosition().x), "cy": mm(p.GetPosition().y),
                         "w": r - l, "h": b - t, "name": fp.GetReference() + "." + p.GetNumber(),
                         "is_0402": footprint_is_inch_0402(fp.GetFPIDAsString()),
                         "round": p.GetShape() == pcbnew.PAD_SHAPE_CIRCLE, "th": p.GetAttribute() == pcbnew.PAD_ATTRIB_PTH,
                         "item": p})
    vias = [{"net": v.GetNetname(), "x": mm(v.GetPosition().x), "y": mm(v.GetPosition().y), "r": mm(v.GetWidth(pcbnew.F_Cu)) / 2, "item": v}
            for v in board.Tracks() if v.GetClass() == "PCB_VIA"]
    segs = []
    for t in board.Tracks():
        if t.GetClass() != "PCB_TRACK":
            continue
        s, e = t.GetStart(), t.GetEnd()
        ax, ay, bx, by = mm(s.x), mm(s.y), mm(e.x), mm(e.y)
        if math.hypot(bx - ax, by - ay) < 1e-6:
            continue
        segs.append({"net": t.GetNetname(), "layer": t.GetLayer(), "ax": ax, "ay": ay, "bx": bx, "by": by, "w": mm(t.GetWidth()), "item": t})

    def pad_at(net, x, y, *, layer):
        for p in pads:
            if not p["item"].IsOnLayer(layer):
                continue
            point = pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y))
            if p["net"] == net and p["item"].HitTest(point, 0, layer):
                return p
        return None

    def via_at(net, x, y, *, layer):
        for v in vias:
            if v["item"].IsOnLayer(layer) and v["net"] == net and math.hypot(x - v["x"], y - v["y"]) <= v["r"]:
                return v
        return None

    zones = [z for z in board.Zones() if not z.GetIsRuleArea()]
    drawings = [d for d in board.GetDrawings()
                if d.GetClass() == "PCB_SHAPE" and d.IsOnCopperLayer()]

    def cap_exposure(pad, s, x, y, ox, oy):
        """Sample the cap outside the pad and adjoining same-net copper.

        Probe 2 nm outside the cap against actual pad, track, via, drawing and
        saved-fill geometry. A clear point beside the cap identifies a local
        exposed boundary candidate. No clear point is not mathematical proof
        of complete coverage: angular sampling is at 5-degree intervals.
        """
        radius = s["w"] / 2
        length = math.hypot(ox - x, oy - y)
        if length == 0:
            return None
        reach = radius + 0.000002
        same_pads = [q["item"] for q in pads if q["net"] == s["net"] and q["item"].IsOnLayer(s["layer"])
                     and q["l"] - reach <= x <= q["r"] + reach and q["t"] - reach <= y <= q["b"] + reach]
        same_tracks = [q for q in segs if q["net"] == s["net"] and q["layer"] == s["layer"]
                       and min(q["ax"], q["bx"]) - q["w"] / 2 - reach <= x <= max(q["ax"], q["bx"]) + q["w"] / 2 + reach
                       and min(q["ay"], q["by"]) - q["w"] / 2 - reach <= y <= max(q["ay"], q["by"]) + q["w"] / 2 + reach]
        same_vias = [v["item"] for v in vias if v["net"] == s["net"] and v["item"].IsOnLayer(s["layer"])
                     and math.hypot(x - v["x"], y - v["y"]) <= reach + v["r"]]
        same_zones = [z for z in zones if z.GetNetname() == s["net"] and z.IsOnLayer(s["layer"])]
        same_drawings = [d for d in drawings if d.GetNetname() == s["net"] and d.IsOnLayer(s["layer"])]

        for step in range(72):
            angle = 2 * math.pi * step / 72
            px, py = x + reach * math.cos(angle), y + reach * math.sin(angle)
            pos = pcbnew.VECTOR2I(pcbnew.FromMM(px), pcbnew.FromMM(py))
            if any(q.HitTest(pos, 0, s["layer"]) for q in same_pads):
                continue
            if any(q["item"].HitTest(pos, 0) for q in same_tracks):
                continue
            if any(v.HitTest(pos, 0) for v in same_vias):
                continue
            if any(d.HitTest(pos, 0) for d in same_drawings):
                continue
            if any(z.HitTestFilledArea(s["layer"], pos, 0) for z in same_zones):
                continue
            return (px, py)
        return None

    findings = defaultdict(list)

    # ---- segment angles ----
    for s in segs:
        dx, dy = s["bx"] - s["ax"], s["by"] - s["ay"]
        ang = math.degrees(math.atan2(abs(dy), abs(dx)))
        if min(abs(ang - a) for a in (0, 45, 90)) > 1.0:
            findings["segment not 0/45/90"].append(f'{s["net"]} {board.GetStandardLayerName(s["layer"])} ({s["ax"]:.2f},{s["ay"]:.2f})->({s["bx"]:.2f},{s["by"]:.2f}) {ang:.1f} deg')

    # ---- corners and junctions ----
    ends = defaultdict(list)
    for i, s in enumerate(segs):
        L = math.hypot(s["bx"] - s["ax"], s["by"] - s["ay"])
        ux, uy = (s["bx"] - s["ax"]) / L, (s["by"] - s["ay"]) / L
        ends[(s["layer"], s["net"], round(s["ax"], 3), round(s["ay"], 3))].append((ux, uy, i, L))
        ends[(s["layer"], s["net"], round(s["bx"], 3), round(s["by"], 3))].append((-ux, -uy, i, L))
    for (layer, net, x, y), dirs in ends.items():
        if len(dirs) < 2:
            continue
        covered = pad_at(net, x, y, layer=layer) or via_at(net, x, y, layer=layer)
        end_radius = max(segs[d[2]]["w"] for d in dirs) / 2
        if covered and "name" in covered:
            depth = pad_boundary_depth(covered["item"], layer, x, y)
            fully_covered = depth is not None and depth + 1e-6 >= end_radius
        elif covered:
            fully_covered = covered["r"] - math.hypot(x - covered["x"], y - covered["y"]) + 1e-6 >= end_radius
        else:
            fully_covered = False
        for i in range(len(dirs)):
            for j in range(i + 1, len(dirs)):
                ux, uy, si, Li = dirs[i]; vx, vy, sj, Lj = dirs[j]
                ang = math.degrees(math.acos(max(-1.0, min(1.0, ux * vx + uy * vy))))
                where = f'{net} {board.GetStandardLayerName(layer)} ({x:.2f},{y:.2f}) angle {ang:.0f}'
                if ang < 89.0:
                    findings["acute centreline: inspect copper outline (inspect)"].append(where + (" (centre under pad or via)" if covered else ""))
                    continue
                if abs(ang - 180) < 1.0:
                    continue
                if abs(ang - 135) < 1.0:
                    # a 45 bend: the chamfer legs must be at least a track width long, unless they end in a pad or via
                    wmax = max(segs[si]["w"], segs[sj]["w"])
                    for sid, Ls in ((si, Li), (sj, Lj)):
                        s = segs[sid]
                        far = (s["bx"], s["by"]) if (round(s["ax"], 3), round(s["ay"], 3)) == (x, y) else (s["ax"], s["ay"])
                        if Ls < wmax - 0.005 and not (pad_at(net, *far, layer=layer) or via_at(net, *far, layer=layer)):
                            findings["token chamfer shorter than the track width"].append(where + f' leg {Ls:.2f} mm, width {wmax:.2f}')
                    continue
                if len(dirs) == 2 and not fully_covered:
                    exposed = []
                    for _, _, sid, _ in dirs:
                        segment = segs[sid]
                        if math.hypot(segment["ax"] - x, segment["ay"] - y) <= math.hypot(segment["bx"] - x, segment["by"] - y):
                            ex, ey, fx, fy = segment["ax"], segment["ay"], segment["bx"], segment["by"]
                        else:
                            ex, ey, fx, fy = segment["bx"], segment["by"], segment["ax"], segment["ay"]
                        exposed.append(cap_exposure(covered, segment, ex, ey, fx, fy))
                    if any(exposed):
                        findings["90 degree corner on a single track"].append(where)
                    else:
                        findings["corner: no exposed samples (inspect)"].append(where)
                elif len(dirs) == 2 and fully_covered:
                    findings["corner covered by pad or via (allowed)"].append(where + f" under {covered['name'] if 'name' in covered else 'via'}")
                # 90 at a junction of 3+ segments: allowed (T-branch)

    # ---- pad entries: centre, axis, end cap inside the outline, necking ----
    touched = defaultdict(list)   # pad name -> segments that touch it
    for s in segs:
        for (x, y, ox, oy) in ((s["ax"], s["ay"], s["bx"], s["by"]), (s["bx"], s["by"], s["ax"], s["ay"])):
            p = pad_at(s["net"], x, y, layer=s["layer"])
            if p is None or p["th"]:
                continue
            if s not in touched[p["name"]]:
                touched[p["name"]].append(s)
            dx, dy = ox - x, oy - y
            L = math.hypot(dx, dy)
            depth = pad_boundary_depth(p["item"], s["layer"], x, y)
            if depth is None:
                findings["unsupported pad shape (inspect)"].append(f'{s["net"]} {p["name"]} at ({x:.2f},{y:.2f})')
            elif depth + 0.005 < s["w"] / 2:
                over = s["w"] / 2 - depth
                exposed = cap_exposure(p, s, x, y, ox, oy)
                if exposed:
                    findings["track end cap outside the pad outline"].append(f'{s["net"]} {p["name"]} at ({x:.2f},{y:.2f}) overhang {over:.3f} mm; boundary candidate ({exposed[0]:.4f},{exposed[1]:.4f})')
                else:
                    findings["end cap: no exposed samples (inspect)"].append(f'{s["net"]} {p["name"]} at ({x:.2f},{y:.2f}) pad-only overhang {over:.3f} mm')
            # A segment wholly inside a pad does not enter the pad at its
            # centreline angle. For a real crossing, use the pad's axes.
            far = pcbnew.VECTOR2I(pcbnew.FromMM(ox), pcbnew.FromMM(oy))
            far_inside = p["item"].HitTest(far, 0, s["layer"])
            ang = math.degrees(math.atan2(dy, dx))
            axis_error = abs((ang + p["item"].GetOrientationDegrees() + 45) % 90 - 45)
            if not p["round"] and not far_inside and axis_error > 1.0:
                findings["track meets pad off its axis"].append(f'{s["net"]} {p["name"]} at ({x:.2f},{y:.2f}) {axis_error:.0f} deg off axis')
            if p["is_0402"] and s["w"] > 0.255 and not far_inside:
                findings["track wider than 0.25 into an 0402 pad"].append(f'{s["net"]} {p["name"]} width {s["w"]:.2f}')
            elif p["is_0402"] and s["w"] > 0.255 and far_inside:
                far_depth = pad_boundary_depth(p["item"], s["layer"], ox, oy)
                if depth is None or far_depth is None or min(depth, far_depth) + 1e-6 < s["w"] / 2:
                    findings["wide internal 0402 segment (inspect)"].append(f'{s["net"]} {p["name"]} width {s["w"]:.2f}; inspect pad-edge copper')
    # every SMD pad with tracks: one of them must end on the centre
    for p in pads:
        if p["th"] or p["name"] not in touched:
            continue
        if not any((abs(s["ax"] - p["cx"]) < 0.02 and abs(s["ay"] - p["cy"]) < 0.02) or (abs(s["bx"] - p["cx"]) < 0.02 and abs(s["by"] - p["cy"]) < 0.02) for s in touched[p["name"]]):
            findings["no track ends on the pad centre"].append(f'{p["name"]} ({p["cx"]:.2f},{p["cy"]:.2f})')

    # ---- vias vs same-net pads, and bends within a via's reach ----
    for v in vias:
        for p in pads:
            if p["net"] != v["net"] or p["th"]:
                continue
            layers = [layer for layer in (pcbnew.F_Cu, pcbnew.B_Cu)
                      if p["item"].IsOnLayer(layer) and v["item"].IsOnLayer(layer)]
            if not layers:
                continue
            gap = min(pad_via_gap(p["item"], v["item"], layer) for layer in layers)
            if gap < CLR - 1e-6:
                findings["via within 0.2 mm of a same-net pad"].append(f'{v["net"]} via ({v["x"]:.2f},{v["y"]:.2f}) {max(gap, 0):.3f} mm from {p["name"]}')
    for (layer, net, x, y), dirs in ends.items():
        if len(dirs) < 2 or via_at(net, x, y, layer=layer):
            continue
        for v in vias:
            if v["item"].IsOnLayer(layer) and v["net"] == net and math.hypot(x - v["x"], y - v["y"]) < v["r"] + 0.3:
                # an approach: one of the segments at this bend ends on the via
                approach = False
                for ux, uy, sid, Ls in dirs:
                    sg = segs[sid]
                    for ex, ey in ((sg["ax"], sg["ay"]), (sg["bx"], sg["by"])):
                        if math.hypot(ex - v["x"], ey - v["y"]) < 0.02:
                            approach = True
                if not approach:
                    findings["bend within a via's reach but not on the via"].append(f'{net} {board.GetStandardLayerName(layer)} ({x:.2f},{y:.2f}) near via ({v["x"]:.2f},{v["y"]:.2f})')

    # ---- copper envelope past 0402 pads: wider caps and teardrops count ----
    copper = []   # (net, layer_id or None, item, bbox)
    for t in board.Tracks():
        copper.append((t.GetNetname(), None if t.GetClass() == "PCB_VIA" else t.GetLayer(), t, t.GetBoundingBox()))
    for d in board.GetDrawings():
        if d.GetClass() == "PCB_SHAPE" and d.IsOnCopperLayer() and d.GetShape() == pcbnew.SHAPE_T_POLY:
            copper.append((d.GetNetname(), d.GetLayer(), d, d.GetBoundingBox()))
    def copper_width(net, layer_id, x, y, ux, uy, include_zones, neck_w):
        """Sample the contiguous same-net copper width across the entry axis.

        The neck, other tracks, via rings and polygons all participate in the
        same union; counting only wider objects misses overlapping copper.
        Disconnected pieces away from the centre do not widen this neck.
        Samples are spaced 0.005 mm and cover at most 0.8 mm.
        """
        nx, ny = -uy, ux
        step = 0.005
        probe = pcbnew.BOX2I(pcbnew.VECTOR2I(pcbnew.FromMM(x - 0.5), pcbnew.FromMM(y - 0.5)), pcbnew.VECTOR2I(pcbnew.FromMM(1.0), pcbnew.FromMM(1.0)))
        near = [(n, l, it) for n, l, it, bb in copper if n == net and (l == layer_id or (l is None and it.IsOnLayer(layer_id))) and probe.Intersects(bb)]
        zs = [z for z in zones if include_zones and z.GetNetname() == net and z.IsOnLayer(layer_id)]
        hits = []
        for index in range(161):
            k = -0.4 + index * step
            pos = pcbnew.VECTOR2I(pcbnew.FromMM(x + nx * k), pcbnew.FromMM(y + ny * k))
            hits.append(any(it.HitTest(pos, 0) for n, l, it in near) or any(z.HitTestFilledArea(layer_id, pos, 0) for z in zs))
        centre = 80
        if not hits[centre]:
            return None
        left = right = centre
        while left > 0 and hits[left - 1]:
            left -= 1
        while right + 1 < len(hits) and hits[right + 1]:
            right += 1
        return (right - left + 1) * step

    for p in pads:
        if p["th"] or p["name"] not in touched:
            continue
        if not p["is_0402"]:
            continue
        want = 0.25
        for s in touched[p["name"]]:
            layer_id = s["layer"]
            # entry direction: from the pad end of the segment towards the other end
            if abs(s["ax"] - p["cx"]) < 0.02 and abs(s["ay"] - p["cy"]) < 0.02:
                ox, oy = s["bx"], s["by"]
            elif abs(s["bx"] - p["cx"]) < 0.02 and abs(s["by"] - p["cy"]) < 0.02:
                ox, oy = s["ax"], s["ay"]
            else:
                continue
            dx, dy = ox - p["cx"], oy - p["cy"]
            L = math.hypot(dx, dy)
            if L < 1e-6 or min(abs(dx), abs(dy)) > 1e-3:
                continue   # only axis entries are necks
            ux, uy = dx / L, dy / L
            edge = pad_exit_distance(p["item"], layer_id, p["cx"], p["cy"], ux, uy)
            if edge is None:
                findings["unsupported pad shape (inspect)"].append(f'{p["net"]} {p["name"]}: cannot locate pad edge')
                continue
            worst = None
            d = 0.0
            while d <= 0.30 + 1e-9:
                x, y = p["cx"] + ux * (edge + d), p["cy"] + uy * (edge + d)
                wdt = copper_width(p["net"], layer_id, x, y, ux, uy, False, want)
                if wdt is None:
                    findings["0402 neck width unestablished (inspect)"].append(f'{p["net"]} {p["name"]}: no connected copper sample at {d:.2f} mm past the pad')
                    break
                if wdt > want + 0.03:
                    worst = (d, wdt); break
                d += 0.01
            if worst:
                findings["neck copper wider than the neck within 0.3 mm of the pad"].append(
                    f'{p["net"]} {p["name"]} {board.GetStandardLayerName(layer_id)}: copper {worst[1]:.2f} mm wide at {worst[0]:.2f} mm past the edge (neck {want:.2f})')

    # ---- the bend before a pad must sit outside the pad outline ----
    for p in pads:
        if p["th"] or p["name"] not in touched or p["round"]:
            continue
        for s in touched[p["name"]]:
            if abs(s["ax"] - p["cx"]) < 0.02 and abs(s["ay"] - p["cy"]) < 0.02:
                bx, by = s["bx"], s["by"]
            elif abs(s["bx"] - p["cx"]) < 0.02 and abs(s["by"] - p["cy"]) < 0.02:
                bx, by = s["ax"], s["ay"]
            else:
                continue
            if via_at(s["net"], bx, by, layer=s["layer"]):
                continue
            depth = pad_boundary_depth(p["item"], s["layer"], bx, by)
            if depth is not None and depth > 0:
                # the far end is inside the pad: only a defect if something continues from it (a bend), not a plain stub end
                cont = [q for q in segs if q is not s and q["net"] == s["net"] and q["layer"] == s["layer"] and
                        ((abs(q["ax"] - bx) < 0.001 and abs(q["ay"] - by) < 0.001) or (abs(q["bx"] - bx) < 0.001 and abs(q["by"] - by) < 0.001))]
                inside = depth
                exposed = cap_exposure(p, s, bx, by, p["cx"], p["cy"]) if cont and inside + 1e-6 < s["w"] / 2 else None
                if cont and inside + 1e-6 >= s["w"] / 2:
                    findings["bend inside a pad, fully covered by its copper (allowed)"].append(f'{s["net"]} {p["name"]} bend at ({bx:.2f},{by:.2f}) {inside:.3f} mm inside')
                elif cont and exposed:
                    findings["bend inside the pad outline"].append(f'{s["net"]} {p["name"]} bend at ({bx:.2f},{by:.2f}) {inside:.3f} mm inside')
                elif cont:
                    findings["bend: no exposed samples (inspect)"].append(f'{s["net"]} {p["name"]} bend at ({bx:.2f},{by:.2f}) {inside:.3f} mm inside')
    return findings


def geometry_snapshot(board):
    """Record actual pad shapes, track endpoints and same-net via clearances.

    Layer IDs are KiCad's numeric IDs. Pad membership and distances come from
    KiCad's effective copper shapes, not the pad bounding boxes. A zero
    clearance includes overlap; the values do not establish solder behaviour.
    """
    tracks = [t for t in board.Tracks() if t.GetClass() == "PCB_TRACK"]
    vias = [v for v in board.Tracks() if v.GetClass() == "PCB_VIA"]
    out = []
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            ref = fp.GetReference() + "." + pad.GetNumber()
            pos, size = pad.GetPosition(), pad.GetSize()
            record = {"pad": ref, "uuid": pad.m_Uuid.AsString(), "net": pad.GetNetname(),
                      "center_mm": [mm(pos.x), mm(pos.y)], "size_mm": [mm(size.x), mm(size.y)],
                      "orientation_deg": pad.GetOrientationDegrees(), "shape": int(pad.GetShape()),
                      "layers": [int(layer) for layer in (pcbnew.F_Cu, pcbnew.B_Cu) if pad.IsOnLayer(layer)],
                      "track_endpoints": [], "near_vias": []}
            for t in tracks:
                layer = t.GetLayer()
                if pad.GetNetname() != t.GetNetname() or not pad.IsOnLayer(layer):
                    continue
                a, b = t.GetStart(), t.GetEnd()
                in_a, in_b = pad.HitTest(a, 0, layer), pad.HitTest(b, 0, layer)
                if not (in_a or in_b):
                    continue
                edge_a = pad_boundary_depth(pad, layer, mm(a.x), mm(a.y)) if in_a else None
                edge_b = pad_boundary_depth(pad, layer, mm(b.x), mm(b.y)) if in_b else None
                record["track_endpoints"].append({
                    "uuid": t.m_Uuid.AsString(), "layer_id": int(layer),
                    "start_mm": [mm(a.x), mm(a.y)], "end_mm": [mm(b.x), mm(b.y)],
                    "start_inside_pad": bool(in_a), "end_inside_pad": bool(in_b),
                    "start_pad_edge_distance_mm": edge_a,
                    "end_pad_edge_distance_mm": edge_b,
                    "width_mm": mm(t.GetWidth())})
            for via in vias:
                if pad.GetNetname() != via.GetNetname():
                    continue
                for layer in (pcbnew.F_Cu, pcbnew.B_Cu):
                    if not pad.IsOnLayer(layer) or not via.IsOnLayer(layer):
                        continue
                    center = via.GetPosition()
                    radius = mm(via.GetWidth(layer)) / 2
                    distance = mm(pad.GetEffectiveShape(layer).Distance(center))
                    gap = pad_via_gap(pad, via, layer)
                    if gap < 0.25:
                        record["near_vias"].append({"uuid": via.m_Uuid.AsString(),
                            "layer_id": int(layer), "center_mm": [mm(center.x), mm(center.y)],
                            "radius_mm": radius, "center_to_pad_copper_mm": distance, "edge_gap_mm": gap})
            out.append(record)
    return out


def inspect_witnesses(board, witnesses):
    """Test supplied points against actual same-net copper on the named layer."""
    pads = [(fp.GetReference() + "." + p.GetNumber(), p)
            for fp in board.GetFootprints() for p in fp.Pads()]
    tracks = list(board.Tracks())
    zones = [z for z in board.Zones() if not z.GetIsRuleArea()]
    drawings = [d for d in board.GetDrawings() if d.GetClass() == "PCB_SHAPE" and d.IsOnCopperLayer()]
    footprint_copper = [(fp.GetReference(), item) for fp in board.GetFootprints() for item in fp.GraphicalItems()
                        if hasattr(item, "IsOnCopperLayer") and item.IsOnCopperLayer()]
    out = []
    for witness in witnesses:
        net, layer = witness["net"], witness["layer_id"]
        point = pcbnew.VECTOR2I(*(pcbnew.FromMM(v) for v in witness["point_mm"]))
        pad_hits = [name for name, p in pads if p.GetNetname() == net and p.IsOnLayer(layer) and p.HitTest(point, 0, layer)]
        track_hits = [t.m_Uuid.AsString() for t in tracks if t.GetClass() == "PCB_TRACK" and
                      t.GetNetname() == net and t.GetLayer() == layer and t.HitTest(point, 0)]
        via_hits = [v.m_Uuid.AsString() for v in tracks if v.GetClass() == "PCB_VIA" and
                    v.GetNetname() == net and v.IsOnLayer(layer) and v.HitTest(point, 0)]
        drawing_hits = [d.m_Uuid.AsString() for d in drawings if d.GetNetname() == net and
                        d.IsOnLayer(layer) and d.HitTest(point, 0)]
        zone_hits = [z.m_Uuid.AsString() for z in zones if z.GetNetname() == net and
                     z.IsOnLayer(layer) and z.HitTestFilledArea(layer, point, 0)]
        footprint_hits = [ref + ":" + item.m_Uuid.AsString() for ref, item in footprint_copper
                          if item.IsOnLayer(layer) and item.HitTest(point, 0)]
        out.append({**witness, "pad_hits": pad_hits, "track_hits": track_hits, "via_hits": via_hits,
                    "drawing_hits": drawing_hits, "zone_hits": zone_hits, "footprint_copper_hits": footprint_hits,
                    "only_target_track": (track_hits == [witness["target_track_uuid"]] and
                                          not (pad_hits or via_hits or drawing_hits or zone_hits or footprint_hits))})
    return out


def main(path: str, output=None, geometry=False, witness_path=None) -> int:
    try:
        board = load(path)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    findings = analyse(board)
    fail_keys = [k for k in findings if not k.endswith(("(allowed)", "(inspect)"))]
    inspect_keys = [k for k in findings if k.endswith("(inspect)")]
    total = sum(len(findings[k]) for k in fail_keys)
    inspect_total = sum(len(findings[k]) for k in inspect_keys)
    print(f"craft check: {total} failing items, {inspect_total} inspection candidates")
    for k in sorted(findings):
        items = findings[k]
        print(f"== {k}: {len(items)}")
        for it in sorted(items):
            print("   " + it)
    if output:
        report = {"board_sha256": hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest(),
                  "checker_sha256": hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(),
                  "reported_failing_items": total, "inspection_candidates": inspect_total, "findings": dict(findings)}
        if geometry:
            report["pad_geometry"] = geometry_snapshot(board)
        if witness_path:
            report["witnesses"] = inspect_witnesses(board, json.loads(pathlib.Path(witness_path).read_text(encoding="utf-8")))
        out_path = pathlib.Path(output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 1 if total else 2 if inspect_total else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("board")
    parser.add_argument("--json", "--output", dest="output")
    parser.add_argument("--geometry", action="store_true")
    parser.add_argument("--witnesses")
    args = parser.parse_args()
    raise SystemExit(main(args.board, args.output, args.geometry, args.witnesses))
