#!/usr/bin/env python3
"""Offline board geometry, rule, and copper brief from a saved KiCad PCB, .kicad_pro, and .kicad_dru.

Pure Python (no KiCad GUI or IPC needed) so routing steps can hand operators exact world pad
coordinates, facing short-edge escape vectors, courtyards, netclasses, custom rules, and
existing copper at startup instead of rediscovering them over slow IPC calls.
"""
from __future__ import annotations

import collections
import math
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import kicad_lay  # noqa: E402
import kicad_route as kr  # noqa: E402
import kicad_rules_geom as rg  # noqa: E402
import kicad_sexp  # noqa: E402
from kicad_route_quality import PadCopper  # noqa: E402


def _fmt(value: float) -> str:
    return rg._fmt_num(value)


def _net_name(node, net_map: dict[str, str]) -> str:
    if isinstance(node, list) and node and node[0] in ("net", "net_name"):
        return rg.resolve_item_net([None, node], net_map)
    return rg.resolve_item_net(node, net_map)


def _point_node(parent, tag: str) -> tuple[float, float] | None:
    node = kicad_sexp.child(parent, tag)
    if node and len(node) >= 3:
        try:
            return (float(node[1]), float(node[2]))
        except (TypeError, ValueError):
            return None
    return None


def _world_pt(fx: float, fy: float, angle_deg: float, lx: float, ly: float) -> tuple[float, float]:
    wx, wy = rg.local_to_world(fx, fy, angle_deg, lx, ly)
    return (round(wx, 4), round(wy, 4))


def _to_world(fx: float, fy: float, cos_f: float, sin_f: float, lx: float, ly: float) -> tuple[float, float]:
    return _world_pt(fx, fy, math.degrees(math.atan2(sin_f, cos_f)), lx, ly)


def _bbox(points: list[tuple[float, float]]) -> list[float] | None:
    if not points:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return [round(min(xs), 4), round(min(ys), 4), round(max(xs), 4), round(max(ys), 4)]


def _in_bbox(points: list[tuple[float, float]], box: list[float]) -> bool:
    x0, y0, x1, y1 = box
    if any(x0 <= px <= x1 and y0 <= py <= y1 for px, py in points):
        return True
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return not (max(xs) < x0 or min(xs) > x1 or max(ys) < y0 or min(ys) > y1)


def _pad_layers(pad_node, copper_layers: list[str]) -> tuple[str, ...]:
    node = kicad_sexp.child(pad_node, "layers")
    if not isinstance(node, list) or len(node) < 2:
        return ()
    return rg.expand_copper_layers([str(tok) for tok in node[1:]], copper_layers)


def _footprint_ref(fp) -> str:
    ref = kicad_sexp.prop(fp, "Reference")
    if ref:
        return str(ref)
    for txt in kicad_sexp.children(fp, "fp_text"):
        if len(txt) >= 3 and str(txt[1]) == "reference":
            return str(txt[2])
    return ""


def _parse_edge_cuts(root) -> tuple[list[float] | None, list[list[float]] | None]:
    points: list[tuple[float, float]] = []
    candidates: list[list[tuple[float, float]]] = []
    chains: list[list[tuple[float, float]]] = []
    for tag in ("gr_line", "gr_rect", "gr_poly", "gr_arc", "gr_circle"):
        for item in kicad_sexp.children(root, tag):
            layer = kicad_sexp.child(item, "layer")
            if not layer or len(layer) < 2 or str(layer[1]) != "Edge.Cuts":
                continue
            if tag == "gr_circle":
                center = _point_node(item, "center")
                end = _point_node(item, "end")
                if center and end:
                    r = math.hypot(end[0] - center[0], end[1] - center[1])
                    pts = [(round(center[0] + r * math.cos(math.radians(a)), 4),
                            round(center[1] + r * math.sin(math.radians(a)), 4))
                           for a in range(0, 360, 15)]
                    points.extend(((center[0] - r, center[1] - r), (center[0] + r, center[1] + r)))
                    candidates.append(pts)
                continue
            if tag == "gr_rect":
                st = _point_node(item, "start")
                en = _point_node(item, "end")
                if st and en:
                    x0, x1 = min(st[0], en[0]), max(st[0], en[0])
                    y0, y1 = min(st[1], en[1]), max(st[1], en[1])
                    rect = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
                    points.extend((st, en))
                    candidates.append(rect)
                continue
            if tag == "gr_line":
                st = _point_node(item, "start")
                en = _point_node(item, "end")
                if st and en:
                    points.extend((st, en))
                    chains.append([st, en])
                continue
            if tag == "gr_arc":
                st = _point_node(item, "start")
                mid = _point_node(item, "mid")
                en = _point_node(item, "end")
                if st and mid and en:
                    sampled = rg.sample_arc_points(st, mid, en)
                    points.extend((st, mid, en))
                    chains.append(sampled)
                continue
            pts = kicad_sexp.child(item, "pts")
            if pts:
                poly_pts = []
                for xy in kicad_sexp.children(pts, "xy"):
                    if len(xy) >= 3:
                        try:
                            pt = (float(xy[1]), float(xy[2]))
                            points.append(pt)
                            poly_pts.append(pt)
                        except (TypeError, ValueError):
                            pass
                if len(poly_pts) >= 3:
                    candidates.append(poly_pts)
    candidates.extend(rg.stitch_outline_edges(chains))
    best_poly = None
    best_area = 1e-4
    for cand in candidates:
        try:
            poly = rg.validate_polygon(cand, "board outline")
        except rg.LayFailure:
            continue
        area = abs(rg.polygon_signed_area(poly))
        if area > best_area:
            best_area = area
            best_poly = [[round(p[0], 4), round(p[1], 4)] for p in poly]
    return _bbox(points), best_poly


def _zone_net(zone_node, net_map: dict[str, str]) -> str:
    return rg.resolve_item_net(zone_node, net_map)


def _zone_connect(zone_node) -> str:
    cp = kicad_sexp.child(zone_node, "connect_pads")
    if cp and len(cp) >= 2 and isinstance(cp[1], str):
        return cp[1]
    return "thermal"


def _extract_zones(root, net_map: dict[str, str]) -> list[dict]:
    zones: list[dict] = []
    for z in kicad_sexp.children(root, "zone"):
        keepout = kicad_sexp.child(z, "keepout")
        name_node = kicad_sexp.child(z, "name")
        name = str(name_node[1]) if name_node and len(name_node) >= 2 else ""
        layers_node = kicad_sexp.child(z, "layers") or kicad_sexp.child(z, "layer")
        layers = [str(tok) for tok in (layers_node[1:] if layers_node else [])]
        pri_node = kicad_sexp.child(z, "priority")
        cl_node = kicad_sexp.child(z, "clearance")
        mt_node = kicad_sexp.child(z, "min_thickness")
        try:
            priority = int(float(pri_node[1])) if pri_node and len(pri_node) >= 2 else 0
        except (TypeError, ValueError):
            priority = 0
        try:
            clearance = round(float(cl_node[1]), 4) if cl_node and len(cl_node) >= 2 else None
        except (TypeError, ValueError):
            clearance = None
        try:
            min_thickness = round(float(mt_node[1]), 4) if mt_node and len(mt_node) >= 2 else None
        except (TypeError, ValueError):
            min_thickness = None
        pts: list[tuple[float, float]] = []
        for poly in kicad_sexp.children(z, "polygon"):
            pts_node = kicad_sexp.child(poly, "pts")
            if pts_node:
                for xy in kicad_sexp.children(pts_node, "xy"):
                    if len(xy) >= 3:
                        try:
                            pts.append((float(xy[1]), float(xy[2])))
                        except (TypeError, ValueError):
                            pass
        filled_layers = sorted({
            str(ln[1])
            for fp in kicad_sexp.children(z, "filled_polygon")
            for ln in (kicad_sexp.child(fp, "layer"),)
            if ln and len(ln) >= 2 and (kicad_sexp.child(fp, "pts") is not None)
        })
        zones.append({
            "kind": "rule_area" if keepout else "pour",
            "name": name,
            "net": "" if keepout else _zone_net(z, net_map),
            "layers": layers,
            "priority": priority,
            "clearance": clearance,
            "min_thickness": min_thickness,
            "connect": _zone_connect(z),
            "outline_bbox": _bbox(pts),
            "filled": bool(filled_layers),
            "filled_layers": filled_layers,
        })
    return zones


def _parse_pro(board_path: pathlib.Path, pro_text: str | None) -> dict:
    rules = rg.load_project_rules(board_path.with_suffix(".kicad_pro"), pro_text=pro_text)
    if not rules:
        return {}
    return {
        "classes": rules["classes"],
        "assignments": rules["assignments"],
        "patterns": rules["patterns"],
        "design_rules": rules["design_rules"],
    }


def _parse_dru(board_path: pathlib.Path, dru_text: str | None) -> str:
    if dru_text is None:
        dru_path = board_path.with_suffix(".kicad_dru")
        if not dru_path.is_file():
            return ""
        try:
            dru_text = dru_path.read_text(encoding="utf-8")
        except OSError:
            return ""
    return dru_text.strip()


def _resolve_pad(
    pin_name: str,
    pads: dict[str, dict],
    *,
    net: str | None = None,
    allow_ambiguous: bool = False,
) -> dict | None:
    exact = pads.get(pin_name)
    if exact is not None:
        return exact
    if "@" not in pin_name:
        if not allow_ambiguous:
            return None
        prefix = pin_name + "@"
        matches = [entry for key, entry in pads.items() if key.startswith(prefix)]
        if net is not None:
            for entry in matches:
                if entry.get("net") == net:
                    return entry
        return matches[0] if matches else None
    base, xy_text = pin_name.split("@", 1)
    parts = [p.strip() for p in xy_text.split(",")]
    if len(parts) != 2:
        return None
    try:
        tx, ty = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    prefix = base + "@"
    best = None
    best_d = 0.050001
    for key, entry in pads.items():
        if key != base and not key.startswith(prefix):
            continue
        cx, cy = entry["center"]
        d = math.hypot(cx - tx, cy - ty)
        if d <= best_d:
            best_d = d
            best = entry
    return best


def extract_board_context(
    board_path: pathlib.Path,
    targets: list[list[str]] = (),
    *,
    centered: bool = False,
    board_text: str | None = None,
    pro_text: str | None = None,
    dru_text: str | None = None,
) -> dict:
    """Extract normalized board geometry, rules, target escapes, and copper from a saved KiCad PCB."""
    if board_text is None:
        try:
            board_text = board_path.read_text(encoding="utf-8")
        except OSError:
            return {}
    if not board_text.strip():
        return {}
    try:
        root = kicad_sexp.parse(board_text)
    except ValueError:
        return {}
    if not isinstance(root, list) or not root or root[0] != "kicad_pcb":
        return {}

    copper_layers = kicad_sexp.copper_layers(root)
    net_map = rg.board_net_map(root)
    outline, outline_polygon = _parse_edge_cuts(root)

    footprints: list[dict] = []
    pads: dict[str, dict] = {}
    pad_candidates: dict[str, list[dict]] = collections.defaultdict(list)
    ambiguous_pads: set[str] = set()
    mount_pads: list[dict] = []

    for fp in kicad_sexp.children(root, "footprint"):
        ref = _footprint_ref(fp)
        fp_layer_node = kicad_sexp.child(fp, "layer")
        fp_layer = str(fp_layer_node[1]) if fp_layer_node and len(fp_layer_node) >= 2 else "F.Cu"
        fp_at = kicad_sexp.child(fp, "at") or ["at", "0", "0", "0"]
        try:
            fx, fy = float(fp_at[1]), float(fp_at[2])
            frot = float(fp_at[3]) if len(fp_at) > 3 else 0.0
        except (IndexError, TypeError, ValueError):
            fx, fy, frot = 0.0, 0.0, 0.0

        crtyd_pts: list[tuple[float, float]] = []
        crtyd_layers: set[str] = set()
        for tag in ("fp_line", "fp_rect", "fp_poly", "fp_arc", "fp_circle"):
            for item in kicad_sexp.children(fp, tag):
                layer_node = kicad_sexp.child(item, "layer")
                layer_name = str(layer_node[1]) if layer_node and len(layer_node) >= 2 else ""
                if layer_name not in ("F.CrtYd", "B.CrtYd"):
                    continue
                crtyd_layers.add(layer_name)
                if tag == "fp_rect":
                    st = _point_node(item, "start")
                    en = _point_node(item, "end")
                    if st and en:
                        for cx in (st[0], en[0]):
                            for cy in (st[1], en[1]):
                                crtyd_pts.append(_world_pt(fx, fy, frot, cx, cy))
                elif tag == "fp_circle":
                    center = _point_node(item, "center")
                    end = _point_node(item, "end")
                    if center and end:
                        r = math.hypot(end[0] - center[0], end[1] - center[1])
                        wc = _world_pt(fx, fy, frot, center[0], center[1])
                        crtyd_pts.extend(((wc[0] - r, wc[1] - r), (wc[0] + r, wc[1] + r)))
                else:
                    for ptag in ("start", "mid", "end"):
                        pt = _point_node(item, ptag)
                        if pt:
                            crtyd_pts.append(_world_pt(fx, fy, frot, pt[0], pt[1]))
                    pts = kicad_sexp.child(item, "pts")
                    if pts:
                        for xy in kicad_sexp.children(pts, "xy"):
                            if len(xy) >= 3:
                                try:
                                    crtyd_pts.append(_world_pt(fx, fy, frot, float(xy[1]), float(xy[2])))
                                except (TypeError, ValueError):
                                    pass

        footprints.append({
            "ref": ref,
            "layer": fp_layer,
            "at": [round(fx, 4), round(fy, 4)],
            "angle_deg": round(frot, 4),
            "crtyd_layers": sorted(crtyd_layers),
            "crtyd_bbox": _bbox(crtyd_pts),
        })

        for pad in kicad_sexp.children(fp, "pad"):
            if len(pad) < 4:
                continue
            num = str(pad[1])
            kind = str(pad[2])
            shape = str(pad[3])
            pad_at = kicad_sexp.child(pad, "at") or ["at", "0", "0"]
            try:
                px, py = float(pad_at[1]), float(pad_at[2])
                prot = float(pad_at[3]) if len(pad_at) > 3 else 0.0
            except (IndexError, TypeError, ValueError):
                continue
            bx, by = _world_pt(fx, fy, frot, px, py)
            off_node = kicad_sexp.child(pad, "offset")
            if off_node and len(off_node) >= 3:
                try:
                    ox, oy = float(off_node[1]), float(off_node[2])
                except (TypeError, ValueError):
                    ox, oy = 0.0, 0.0
                if ox or oy:
                    bx, by = _world_pt(bx, by, prot, ox, oy)
            size_node = kicad_sexp.child(pad, "size")
            if not size_node or len(size_node) < 3:
                continue
            try:
                sx, sy = float(size_node[1]), float(size_node[2])
            except (TypeError, ValueError):
                continue
            layers = _pad_layers(pad, copper_layers)
            net = _net_name(kicad_sexp.child(pad, "net"), net_map)
            rr = kicad_sexp.child(pad, "roundrect_rratio")
            try:
                round_ratio = float(rr[1]) if rr and len(rr) > 1 else (0.25 if shape == "roundrect" else 0.0)
            except (TypeError, ValueError):
                round_ratio = 0.25 if shape == "roundrect" else 0.0

            if num:
                if not ref:
                    continue
                name = f"{ref}.{num}"
                entry = {
                    "name": name,
                    "ref": ref,
                    "pad": num,
                    "kind": kind,
                    "shape": shape,
                    "center": (bx, by),
                    "size": (round(sx, 4), round(sy, 4)),
                    "angle_deg": round(prot, 4),
                    "layers": layers,
                    "net": net,
                    "round_ratio": round_ratio,
                }
                if name in pads:
                    ambiguous_pads.add(name)
                else:
                    pads[name] = entry
                pad_candidates[name].append(entry)
            elif layers:
                mount_pads.append({
                    "ref": ref,
                    "kind": kind,
                    "shape": shape,
                    "center": (bx, by),
                    "size": (round(sx, 4), round(sy, 4)),
                    "angle_deg": round(prot, 4),
                    "layers": layers,
                    "net": net,
                })

    for dup in ambiguous_pads:
        pads.pop(dup, None)
        for entry in pad_candidates.get(dup, ()):
            cx, cy = entry["center"]
            coord_name = f"{dup}@{_fmt(cx)},{_fmt(cy)}"
            sub = dict(entry)
            sub["name"] = coord_name
            if coord_name not in pads:
                pads[coord_name] = sub

    target_details = []
    for target in targets:
        if len(target) < 3:
            continue
        from_name, to_name, net = str(target[0]), str(target[1]), str(target[2])
        pad_a = _resolve_pad(from_name, pads, net=net, allow_ambiguous=not centered)
        pad_b = _resolve_pad(to_name, pads, net=net, allow_ambiguous=not centered)
        info_a = _pad_info(pad_a) if pad_a else None
        info_b = _pad_info(pad_b) if pad_b else None
        esc_a = esc_b = None
        if info_a and info_b:
            try:
                esc_a = kicad_lay.facing_escape(info_a, info_b.copper.center)
            except kicad_lay.LayFailure:
                esc_a = None
            try:
                esc_b = kicad_lay.facing_escape(info_b, info_a.copper.center)
            except kicad_lay.LayFailure:
                esc_b = None
        target_details.append({
            "from": from_name,
            "to": to_name,
            "net": net,
            "pad_a": pad_a,
            "pad_b": pad_b,
            "escape_a": esc_a,
            "escape_b": esc_b,
        })

    copper_items: list[dict] = []
    for seg in kicad_sexp.children(root, "segment"):
        st = _point_node(seg, "start")
        en = _point_node(seg, "end")
        w_node = kicad_sexp.child(seg, "width")
        l_node = kicad_sexp.child(seg, "layer")
        if st and en and w_node and len(w_node) >= 2 and l_node and len(l_node) >= 2:
            copper_items.append({
                "kind": "track",
                "start": (round(st[0], 4), round(st[1], 4)),
                "end": (round(en[0], 4), round(en[1], 4)),
                "width": round(float(w_node[1]), 4),
                "layer": str(l_node[1]),
                "net": _net_name(kicad_sexp.child(seg, "net"), net_map),
                "points": [st, en],
            })
    for arc in kicad_sexp.children(root, "arc"):
        st = _point_node(arc, "start")
        mid = _point_node(arc, "mid")
        en = _point_node(arc, "end")
        w_node = kicad_sexp.child(arc, "width")
        l_node = kicad_sexp.child(arc, "layer")
        if st and mid and en and w_node and len(w_node) >= 2 and l_node and len(l_node) >= 2:
            copper_items.append({
                "kind": "arc",
                "start": (round(st[0], 4), round(st[1], 4)),
                "mid": (round(mid[0], 4), round(mid[1], 4)),
                "end": (round(en[0], 4), round(en[1], 4)),
                "width": round(float(w_node[1]), 4),
                "layer": str(l_node[1]),
                "net": _net_name(kicad_sexp.child(arc, "net"), net_map),
                "points": [st, mid, en],
            })
    for via in kicad_sexp.children(root, "via"):
        at = _point_node(via, "at")
        sz = kicad_sexp.child(via, "size")
        dr = kicad_sexp.child(via, "drill")
        if at and sz and len(sz) >= 2 and dr and len(dr) >= 2:
            copper_items.append({
                "kind": "via",
                "at": (round(at[0], 4), round(at[1], 4)),
                "size": round(float(sz[1]), 4),
                "drill": round(float(dr[1]), 4),
                "net": _net_name(kicad_sexp.child(via, "net"), net_map),
                "points": [at],
            })

    zones = _extract_zones(root, net_map)
    pro = _parse_pro(board_path, pro_text)
    dru = _parse_dru(board_path, dru_text)

    if not any((outline, footprints, pads, mount_pads, pro, dru, copper_items, zones)):
        return {}

    return {
        "board": board_path.name,
        "copper_layers": copper_layers,
        "outline": outline,
        "outline_polygon": outline_polygon,
        "footprints": footprints,
        "pads": pads,
        "mount_pads": mount_pads,
        "targets": target_details,
        "centered": centered,
        "pro": pro,
        "dru": dru,
        "copper": copper_items,
        "zones": zones,
    }


def _pad_info(entry: dict) -> kr.PadInfo:
    copper = PadCopper(
        center=entry["center"],
        size=entry["size"],
        angle_deg=entry["angle_deg"],
        shape=entry["shape"],
        round_ratio=entry["round_ratio"],
        label=entry["name"],
    )
    return kr.PadInfo(
        name=entry["name"],
        net=entry["net"],
        x=entry["center"][0],
        y=entry["center"][1],
        layers=entry["layers"],
        half=min(entry["size"]) / 2.0,
        through=len(entry["layers"]) > 1,
        size=entry["size"],
        angle=entry["angle_deg"],
        round=entry["shape"] in ("oval", "circle"),
        copper=copper,
    )


def _format_escape(label: str, pad: dict, esc: dict | None) -> str:
    cx, cy = pad["center"]
    sx, sy = pad["size"]
    layers = "/".join(pad["layers"]) or "?"
    base = f"{label} ({_fmt(cx)},{_fmt(cy)}) {_fmt(sx)}x{_fmt(sy)} {pad['angle_deg']:.0f}deg [{layers}]"
    if not esc:
        return base
    ex, ey = esc["edge"]
    px, py = esc["escape"]
    ux, uy = esc["unit"]
    return (
        f"{base} short_edge=({_fmt(ex)},{_fmt(ey)}) esc=({_fmt(px)},{_fmt(py)}) "
        f"dir=({_fmt(ux)},{_fmt(uy)})"
    )


def _copper_sort_key(item: dict) -> tuple:
    net = item["net"] or "<unconnected>"
    if item["kind"] == "via":
        x, y = item["at"]
        return (x, y, x, y, net)
    xs = [p[0] for p in item["points"]]
    ys = [p[1] for p in item["points"]]
    return (min(xs), min(ys), max(xs), max(ys), net)


def _format_copper_item(item: dict) -> str:
    net = item["net"] or "<unconnected>"
    if item["kind"] == "via":
        x, y = item["at"]
        return f"{net} ({_fmt(x)},{_fmt(y)}) {_fmt(item['size'])}/{_fmt(item['drill'])}"
    if item["kind"] == "arc":
        x1, y1 = item["start"]
        xm, ym = item["mid"]
        x2, y2 = item["end"]
        return f"{net} arc ({_fmt(x1)},{_fmt(y1)})-({_fmt(xm)},{_fmt(ym)})-({_fmt(x2)},{_fmt(y2)}) w={_fmt(item['width'])}"
    x1, y1 = item["start"]
    x2, y2 = item["end"]
    return f"{net} ({_fmt(x1)},{_fmt(y1)})->({_fmt(x2)},{_fmt(y2)}) w={_fmt(item['width'])}"


def _allocate_copper_quotas(
    groups: list[tuple[str, list[dict]]],
    max_copper_items: int,
    active_count: int,
) -> list[int] | None:
    if active_count <= max_copper_items:
        return None
    rem = max(0, max_copper_items)
    quotas = [0] * len(groups)
    layer_indices = [idx for idx, (name, _) in enumerate(groups) if name != "Vias"]
    for idx, (name, items) in enumerate(groups):
        if name == "Vias":
            cap = min(len(items), rem if not layer_indices else max(1, rem // 2) if rem > 0 else 0)
            quotas[idx] = cap
            rem -= cap
            break
    while rem > 0:
        avail = [idx for idx in layer_indices if quotas[idx] < len(groups[idx][1])]
        if not avail:
            break
        step = max(1, rem // len(avail))
        progressed = False
        for idx in avail:
            if rem <= 0:
                break
            add = min(step, rem, len(groups[idx][1]) - quotas[idx])
            if add > 0:
                quotas[idx] += add
                rem -= add
                progressed = True
        if not progressed:
            break
    return quotas


def _compact_context_text(
    sections: dict[str, list[str]],
    ctx: dict,
    shown_pads: list[dict],
    target_refs: set[str],
    budget: int,
) -> str:
    work = {name: list(lines) for name, lines in sections.items()}

    def _join() -> str:
        order = ("header", "footprints", "pro", "dru", "targets", "pads", "copper", "zones")
        out: list[str] = []
        for name in order:
            out.extend(work.get(name) or ())
        return "\n".join(out)

    if len(_join()) <= budget:
        return _join()

    # Step 1: summarize optional background sections while keeping target pads, copper, and zones intact.
    if work.get("dru"):
        work["dru"] = ["", "### Custom rules (.kicad_dru)", f"- Custom rules omitted in inline brief ({len(ctx['dru'])} chars)"]
    if work.get("pro"):
        work["pro"] = ["", "### Project defaults (.kicad_pro)", f"- {len(ctx['pro'].get('classes') or [])} netclasses (see full brief)"]
    if work.get("footprints") and len(work["footprints"]) > 4:
        refs = ", ".join(fp["ref"] or "?" for fp in ctx["footprints"][:30])
        more = f" (+{len(ctx['footprints']) - 30} more)" if len(ctx["footprints"]) > 30 else ""
        work["footprints"] = ["", f"### Footprints ({len(ctx['footprints'])} total): {refs}{more}"]
    if work.get("pads") and target_refs:
        target_pad_lines = [line for line in work["pads"][1:] if any(line.startswith(f"- {ref}.") for ref in target_refs)]
        if target_pad_lines:
            work["pads"] = ["", f"### Target component pads ({len(target_pad_lines)} shown)"] + target_pad_lines
    if len(_join()) <= budget:
        return _join()

    # Step 2: drop background sections progressively, protecting header, targets, copper, and zones.
    for drop_name in ("dru", "pro", "footprints", "pads"):
        if len(_join()) <= budget:
            break
        work[drop_name] = []

    text = _join()
    if len(text) <= budget:
        return text
    cut = text.rfind("\n", 0, budget)
    if cut <= 0:
        cut = budget
    return text[:cut].rstrip()


def render_board_context(
    board_path: pathlib.Path,
    targets: list[list[str]] = (),
    *,
    centered: bool = False,
    board_text: str | None = None,
    pro_text: str | None = None,
    dru_text: str | None = None,
    max_chars: int = 18_000,
    max_copper_items: int = 250,
    full_path: pathlib.Path | None = None,
) -> tuple[str, str]:
    """Render `(inline_text, full_text)` board context brief, or `("", "")` when nothing meaningful exists."""
    ctx = extract_board_context(
        board_path,
        targets,
        centered=centered,
        board_text=board_text,
        pro_text=pro_text,
        dru_text=dru_text,
    )
    if not ctx:
        return "", ""

    sections: dict[str, list[str]] = {
        "header": [],
        "footprints": [],
        "pro": [],
        "dru": [],
        "targets": [],
        "pads": [],
        "copper": [],
        "zones": [],
    }
    header_lines = sections["header"]
    header_lines.append("## Auto-generated board geometry brief (world mm, KiCad Y-down)")
    layers = ", ".join(ctx["copper_layers"]) or "unknown"
    outline = ctx["outline"]
    poly = ctx.get("outline_polygon")
    if outline:
        x0, y0, x1, y1 = outline
        header_lines.append(
            f"Layers: {layers} | Edge.Cuts: x=[{_fmt(x0)}..{_fmt(x1)}], y=[{_fmt(y0)}..{_fmt(y1)}] "
            f"({_fmt(x1 - x0)} x {_fmt(y1 - y0)} mm)"
        )
        if poly and not (len(poly) == 4 and len({p[0] for p in poly}) == 2 and len({p[1] for p in poly}) == 2):
            verts = ", ".join(f"({p[0]},{p[1]})" for p in poly[:24])
            extra = f" (+{len(poly) - 24} more)" if len(poly) > 24 else ""
            header_lines.append(f"Edge.Cuts polygon ({len(poly)} vertices): {verts}{extra}")
    else:
        header_lines.append(f"Layers: {layers}")

    if ctx["footprints"]:
        fp_lines = sections["footprints"]
        fp_lines.append("\n### Footprints & courtyards")
        for fp in ctx["footprints"]:
            box = fp["crtyd_bbox"]
            crtyd = (
                f"crtyd({','.join(fp['crtyd_layers'])}): x=[{_fmt(box[0])}..{_fmt(box[2])}], y=[{_fmt(box[1])}..{_fmt(box[3])}]"
                if box
                else "crtyd: none"
            )
            fp_lines.append(
                f"- {fp['ref'] or '?'}: {fp['layer']} at ({_fmt(fp['at'][0])},{_fmt(fp['at'][1])}) "
                f"rot={_fmt(fp['angle_deg'])}deg | {crtyd}"
            )

    pro = ctx["pro"]
    if pro:
        pro_lines = sections["pro"]
        pro_lines.append("\n### Project defaults (.kicad_pro)")
        for cls in pro.get("classes") or []:
            pro_lines.append(
                f"- Netclass {cls['name'] or 'Default'}: track={_fmt(cls['track_width'] or 0)} mm, "
                f"via={_fmt(cls['via_diameter'] or 0)}/{_fmt(cls['via_drill'] or 0)} mm, "
                f"clearance={_fmt(cls['clearance'] or 0)} mm"
            )
        if pro.get("assignments"):
            pairs = ", ".join(f"{k}->{v}" for k, v in sorted(pro["assignments"].items())[:20])
            pro_lines.append(f"- Netclass assignments: {pairs}")
        if pro.get("patterns"):
            pats = ", ".join(f"{p.get('pattern')}->{p.get('netclass')}" for p in pro["patterns"][:20] if isinstance(p, dict))
            if pats:
                pro_lines.append(f"- Netclass patterns: {pats}")
        if pro.get("design_rules"):
            rules_text = ", ".join(f"{k}={_fmt(v)}" for k, v in pro["design_rules"].items())
            pro_lines.append(f"- Board design rules: {rules_text}")

    dru = ctx["dru"]
    if dru:
        dru_lines = sections["dru"]
        dru_lines.append("\n### Custom rules (.kicad_dru)")
        if len(dru) > 4000:
            dru_lines.append(dru[:4000].rstrip() + "\n[...truncated]")
        else:
            dru_lines.append(dru)

    if ctx["targets"]:
        target_lines = sections["targets"]
        t_header = "\n### Target connections"
        if centered:
            t_header += " (Pad exits: centered — precomputed short-edge midpoints & escape vectors)"
        target_lines.append(t_header)
        for t in ctx["targets"]:
            a_desc = _format_escape(t["from"], t["pad_a"], t["escape_a"]) if t["pad_a"] else f"{t['from']} (not found)"
            b_desc = _format_escape(t["to"], t["pad_b"], t["escape_b"]) if t["pad_b"] else f"{t['to']} (not found)"
            target_lines.append(f"- {t['net']}: {a_desc}  -->  {b_desc}")

    target_pts = [
        p["center"]
        for t in ctx["targets"]
        for p in (t["pad_a"], t["pad_b"])
        if p is not None
    ]
    raw_box = _bbox(target_pts)
    region_box = (
        [raw_box[0] - 8.0, raw_box[1] - 8.0, raw_box[2] + 8.0, raw_box[3] + 8.0]
        if raw_box
        else None
    )
    target_refs = {
        name.split(".", 1)[0]
        for t in ctx["targets"]
        for name in (t["from"], t["to"])
        if "." in name
    }

    pads_by_name = ctx["pads"]
    if len(pads_by_name) <= 80 or not region_box:
        shown_pads = list(pads_by_name.values())
    else:
        shown_pads = [
            p for p in pads_by_name.values()
            if p["ref"] in target_refs or _in_bbox([p["center"]], region_box)
        ]

    shown_refs = {p["ref"] for p in shown_pads} | target_refs
    shown_mounts = [m for m in ctx["mount_pads"] if not shown_refs or m["ref"] in shown_refs]

    if shown_pads or shown_mounts:
        pad_lines = sections["pads"]
        pad_lines.append(f"\n### Pads ({len(shown_pads)} of {len(pads_by_name)} named pads)")
        for p in shown_pads:
            cx, cy = p["center"]
            sx, sy = p["size"]
            net = p["net"] or "<unconnected>"
            layers_str = "/".join(p["layers"]) or "?"
            pad_lines.append(
                f"- {p['name']} ({net}): ({_fmt(cx)},{_fmt(cy)}) {_fmt(sx)}x{_fmt(sy)} "
                f"{p['angle_deg']:.0f}deg [{layers_str}]"
            )
        for m in shown_mounts:
            cx, cy = m["center"]
            sx, sy = m["size"]
            net = m["net"] or "<unconnected>"
            layers_str = "/".join(m["layers"]) or "?"
            pad_lines.append(
                f"- {m['ref']}.mount ({net}, {m['kind']}): ({_fmt(cx)},{_fmt(cy)}) {_fmt(sx)}x{_fmt(sy)} "
                f"{m['angle_deg']:.0f}deg [{layers_str}]"
            )

    copper = ctx["copper"]
    if copper:
        copper_lines = sections["copper"]
        if region_box is not None:
            active_items = [it for it in copper if _in_bbox(it["points"], region_box)]
            outside_items = [it for it in copper if not _in_bbox(it["points"], region_box)]
            copper_lines.append(
                f"\n### Existing copper ({len(active_items)} in active box "
                f"x=[{_fmt(region_box[0])}..{_fmt(region_box[2])}], y=[{_fmt(region_box[1])}..{_fmt(region_box[3])}]; "
                f"{len(copper)} total)"
            )
        else:
            active_items = list(copper)
            outside_items = []
            copper_lines.append(f"\n### Existing copper ({len(copper)} items)")

        vias = sorted([it for it in active_items if it["kind"] == "via"], key=_copper_sort_key)
        by_layer: dict[str, list[dict]] = collections.defaultdict(list)
        for item in active_items:
            if item["kind"] != "via":
                by_layer[item["layer"]].append(item)
        layer_order = [layer for layer in ctx["copper_layers"] if layer in by_layer] + sorted(
            layer for layer in by_layer if layer not in ctx["copper_layers"]
        )
        groups: list[tuple[str, list[dict]]] = []
        if vias:
            groups.append(("Vias", vias))
        for layer in layer_order:
            groups.append((layer, sorted(by_layer[layer], key=_copper_sort_key)))

        quotas = _allocate_copper_quotas(groups, max_copper_items, len(active_items))
        for idx, (group_name, items) in enumerate(groups):
            picked = items if quotas is None else items[:quotas[idx]]
            if picked:
                formatted = "; ".join(_format_copper_item(it) for it in picked)
                suffix = f" (+{len(items) - len(picked)} more)" if len(picked) < len(items) else ""
                copper_lines.append(f"- {group_name} ({len(items)}): {formatted}{suffix}")
            elif items:
                copper_lines.append(f"- {group_name} ({len(items)}): (+{len(items)} more)")

        if outside_items:
            out_by_net: dict[str, list[dict]] = collections.defaultdict(list)
            for item in outside_items:
                out_by_net[item["net"] or "<unconnected>"].append(item)
            summarized = []
            for net in sorted(out_by_net):
                items = out_by_net[net]
                tracks = sum(1 for it in items if it["kind"] != "via")
                vias_cnt = sum(1 for it in items if it["kind"] == "via")
                summarized.append(f"{net} ({tracks} tracks, {vias_cnt} vias)")
            copper_lines.append(f"- Outside active box (summarized, {len(outside_items)} items): " + "; ".join(summarized))

    zones = ctx.get("zones") or []
    if zones:
        zone_lines = sections["zones"]
        zone_lines.append(f"\n### Existing zones ({len(zones)} total: pours & rule areas)")
        for z in zones:
            layers_str = ",".join(z["layers"]) or "?"
            box = z["outline_bbox"]
            bbox_str = (
                f"x=[{_fmt(box[0])}..{_fmt(box[2])}], y=[{_fmt(box[1])}..{_fmt(box[3])}]"
                if box
                else "no outline"
            )
            name_part = f" name={z['name']!r}" if z["name"] else ""
            if z["kind"] == "rule_area":
                zone_lines.append(f"- rule_area ({layers_str}){name_part}: {bbox_str}")
            else:
                state = "filled" if z["filled"] else "unfilled"
                zone_lines.append(
                    f"- pour {z['net'] or '<no-net>'} ({layers_str}){name_part}: prio={z['priority']}, "
                    f"connect={z['connect']}, {state}, {bbox_str}"
                )

    full_lines: list[str] = []
    for key in ("header", "footprints", "pro", "dru", "targets", "pads", "copper", "zones"):
        full_lines.extend(sections[key])
    full_text = "\n".join(full_lines)
    if len(full_text) <= max_chars:
        return full_text, full_text

    note = (
        f"\n[Board geometry summary truncated; read full context at {full_path}]"
        if full_path is not None
        else "\n[Board geometry summary truncated]"
    )
    budget = max(0, max_chars - len(note))
    compact = _compact_context_text(sections, ctx, shown_pads, target_refs, budget)
    return compact + note, full_text
