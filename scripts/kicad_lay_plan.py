#!/usr/bin/env python3
"""Pure route, via, pour, and DRC-diff planning logic shared by kicad_lay.py."""
from __future__ import annotations

import collections
import dataclasses
import math
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import kicad_route as kr  # noqa: E402
import kicad_rules_geom as rg  # noqa: E402
import pad_exits  # noqa: E402
from kicad_route_quality import ANGLE_TOLERANCE_MM, compass_direction  # noqa: E402
from kicad_rules_geom import (  # noqa: E402
    NM,
    LayFailure,
    inset_polygon,
    same_pt,
    snap_pt,
    to_mm,
    to_nm,
    validate_polygon,
)

DEFAULT_TRACK_MM = 0.25
DEFAULT_VIA_DIA_MM = 0.6
DEFAULT_VIA_DRILL_MM = 0.3
DEFAULT_ESCAPE_MM = 0.25
DEFAULT_ZONE_MIN_THICKNESS_MM = 0.2
DEFAULT_ZONE_MIN_ISLAND_MM2 = 10.0
DEFAULT_THERMAL_GAP_MM = 0.5
DEFAULT_THERMAL_BRIDGE_WIDTH_MM = 0.5
ENDPOINT_EPS_MM = 1e-4
ZONE_CONNECT_MODES = ("thermal", "solid", "pth_thermal", "none")
ZONE_ISLAND_MODES = ("always", "area", "never")


@dataclasses.dataclass(frozen=True)
class PlannedSegment:
    start: tuple[float, float]
    end: tuple[float, float]
    layer: str
    width: float
    net: str


@dataclasses.dataclass(frozen=True)
class PlannedVia:
    at: tuple[float, float]
    diameter: float
    drill: float
    net: str


@dataclasses.dataclass(frozen=True)
class PlannedPour:
    net: str
    layers: tuple[str, ...]
    outline: tuple[tuple[float, float], ...]
    holes: tuple[tuple[tuple[float, float], ...], ...] = ()
    name: str = ""
    priority: int = 0
    clearance: float | None = None
    min_thickness: float = DEFAULT_ZONE_MIN_THICKNESS_MM
    connect: str = "thermal"
    thermal_gap: float | None = None
    thermal_bridge_width: float | None = None
    island_mode: str = "always"
    min_island_area: float = DEFAULT_ZONE_MIN_ISLAND_MM2
    replace: bool = False
    replace_ids: tuple[str, ...] = ()


def facing_escape(pad: kr.PadInfo, other_center: tuple[float, float], escape_mm: float = DEFAULT_ESCAPE_MM) -> dict:
    """Exact perpendicular short-edge escape vector and point for an elongated SMD pad facing other_center."""
    copper = pad.copper
    if copper is None:
        raise LayFailure("bad_request", f"{pad.name}: missing pad copper geometry")
    sx, sy = copper.size
    if abs(sx - sy) < pad_exits.EPS or min(sx, sy) <= 0:
        raise LayFailure("bad_request", f"{pad.name}: pad has no unique short edge")
    if copper.shape not in pad_exits.SUPPORTED_SHAPES:
        raise LayFailure("bad_request", f"{pad.name}: centered exits require rect, roundrect or oval SMD pad (got {copper.shape})")
    cx, cy = copper.center
    angle = math.radians(copper.angle_deg)
    dx, dy = other_center[0] - cx, other_center[1] - cy
    u, v = rg.world_to_local(cx, cy, copper.angle_deg, other_center[0], other_center[1])
    if sy > sx:
        toward = v
        half_long = sy / 2.0
        if abs(toward) < pad_exits.EPS:
            raise LayFailure("bad_request", f"{pad.name}: neither short edge faces ({other_center[0]:.3f}, {other_center[1]:.3f})")
        sign = 1.0 if toward > 0 else -1.0
        nx, ny = sign * math.sin(angle), sign * math.cos(angle)
    else:
        toward = u
        half_long = sx / 2.0
        if abs(toward) < pad_exits.EPS:
            raise LayFailure("bad_request", f"{pad.name}: neither short edge faces ({other_center[0]:.3f}, {other_center[1]:.3f})")
        sign = 1.0 if toward > 0 else -1.0
        nx, ny = sign * math.cos(angle), -sign * math.sin(angle)
    edge_dist = half_long
    dist = edge_dist + max(0.05, float(escape_mm))
    # Do not overshoot past the facing point along the axis when pads are close together.
    proj = dx * nx + dy * ny
    if proj > edge_dist + 0.05:
        dist = min(dist, max(edge_dist + 0.05, proj * 0.5))
    edge = snap_pt((cx + nx * edge_dist, cy + ny * edge_dist))
    esc = snap_pt((cx + nx * dist, cy + ny * dist))
    direction = compass_direction((cx, cy), esc)
    return {
        "center": snap_pt((cx, cy)),
        "edge": edge,
        "escape": esc,
        "unit": (round(nx, 6), round(ny, 6)),
        "compass": direction,
        "half_long_mm": round(half_long, 6),
    }


def _corner_score(prev_dir: tuple[int, int] | None, legs: list[tuple[tuple[float, float], tuple[float, float], tuple[int, int]]],
                  next_dir: tuple[int, int] | None) -> tuple[int, int, int]:
    """Higher score is better: prefer obtuse 135-deg turns (dot > 0) over 90-deg (dot == 0) and avoid reversals."""
    dots = []
    if prev_dir is not None and legs:
        dots.append(prev_dir[0] * legs[0][2][0] + prev_dir[1] * legs[0][2][1])
    for i in range(len(legs) - 1):
        d1, d2 = legs[i][2], legs[i + 1][2]
        dots.append(d1[0] * d2[0] + d1[1] * d2[1])
    if next_dir is not None and legs:
        dots.append(legs[-1][2][0] * next_dir[0] + legs[-1][2][1] * next_dir[1])
    if not dots:
        return (2, 2, 0)
    return (
        min(dots),
        sum(1 for d in dots if d > 0) - sum(1 for d in dots if d == 0) - 4 * sum(1 for d in dots if d < 0),
        -len(legs),
    )


def expand_45_leg(a: tuple[float, float], b: tuple[float, float],
                  prev_dir: tuple[int, int] | None = None,
                  next_dir: tuple[int, int] | None = None) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """Expand a point-to-point hop into 1 or 2 H/V/45-degree segments with exact integer-nm corners."""
    ax, ay = to_nm(a[0]), to_nm(a[1])
    bx, by = to_nm(b[0]), to_nm(b[1])
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return []
    tol_nm = to_nm(ANGLE_TOLERANCE_MM)
    abs_dx, abs_dy = abs(dx), abs(dy)
    if abs_dx <= tol_nm or abs_dy <= tol_nm or abs(abs_dx - abs_dy) <= tol_nm:
        pa, pb = (to_mm(ax), to_mm(ay)), (to_mm(bx), to_mm(by))
        if compass_direction(pa, pb) is not None:
            return [(pa, pb)]
    sx = 1 if dx > 0 else -1
    sy = 1 if dy > 0 else -1
    diag = min(abs_dx, abs_dy)
    vx, vy = sx * diag, sy * diag
    rx, ry = dx - vx, dy - vy

    a1_mid = (ax + rx, ay + ry)
    b1_mid = (ax + vx, ay + vy)

    candidates = []
    for bias, mid in enumerate((a1_mid, b1_mid)):
        raw_pairs = [((to_mm(ax), to_mm(ay)), (to_mm(mid[0]), to_mm(mid[1]))),
                     ((to_mm(mid[0]), to_mm(mid[1])), (to_mm(bx), to_mm(by)))]
        legs = []
        valid = True
        for p0, p1 in raw_pairs:
            if same_pt(p0, p1):
                continue
            d = compass_direction(p0, p1)
            if d is None or d == (0, 0):
                valid = False
                break
            legs.append((p0, p1, d))
        if valid and legs:
            candidates.append((_corner_score(prev_dir, legs, next_dir), -bias, legs))
    if not candidates:
        raise LayFailure("bad_geometry", f"could not decompose {a} -> {b} into 45-degree segments")
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [(p0, p1) for p0, p1, _ in candidates[0][2]]


def merge_collinear(segments: list[PlannedSegment]) -> list[PlannedSegment]:
    """Merge consecutive collinear same-layer, same-width segments."""
    merged: list[PlannedSegment] = []
    for seg in segments:
        if same_pt(seg.start, seg.end):
            continue
        d = compass_direction(seg.start, seg.end)
        if d is None or d == (0, 0):
            raise LayFailure("bad_geometry", f"non-45-degree segment {seg.start} -> {seg.end}")
        if merged:
            prev = merged[-1]
            pd = compass_direction(prev.start, prev.end)
            if (prev.layer == seg.layer and prev.net == seg.net and to_nm(prev.width) == to_nm(seg.width)
                    and same_pt(prev.end, seg.start) and pd == d):
                merged[-1] = dataclasses.replace(prev, end=seg.end)
                continue
        merged.append(seg)
    return merged


def _corner_radius(copper) -> float:
    if copper.shape == "rect":
        return 0.0
    if copper.shape == "roundrect":
        if not (0.0 <= copper.round_ratio <= 0.5):
            raise LayFailure("bad_request", f"unsupported roundrect radius ratio {copper.round_ratio}")
        return min(copper.size) * copper.round_ratio
    if copper.shape == "oval":
        return min(copper.size) / 2.0
    raise LayFailure("bad_request", f"centered exits require rect, roundrect or oval pad (got {copper.shape})")


def verify_centered_exits(pad: kr.PadInfo, other_center: tuple[float, float], layer_segments: list[PlannedSegment],
                          vias: list[PlannedVia] | tuple[PlannedVia, ...] = ()) -> None:
    """Run pad_exits.crossing_report on planned segments at one pad before creating copper in KiCad."""
    copper = pad.copper
    if copper is None:
        raise LayFailure("pad_exit_failed", f"{pad.name}: missing copper geometry")
    sx, sy = copper.size
    if abs(sx - sy) < pad_exits.EPS or min(sx, sy) <= 0:
        raise LayFailure("pad_exit_failed", f"{pad.name}: pad has no unique short edge")
    radius = _corner_radius(copper)
    cx, cy = copper.center
    pad_hx, pad_hy = rg.rotated_pad_aabb_half(sx, sy, copper.angle_deg)
    for via in vias:
        vx, vy = via.at
        vr = via.diameter / 2.0
        if abs(vx - cx) <= pad_hx + vr + 1e-4 and abs(vy - cy) <= pad_hy + vr + 1e-4:
            raise LayFailure(
                "pad_exit_failed",
                f"{pad.name}: via at ({vx:.3f}, {vy:.3f}) diameter {via.diameter:.3f} mm touches pad edge "
                "(move via clear of pad bounding box)",
                pad=pad.name,
                via=[vx, vy],
            )

    def local(x: float, y: float) -> tuple[float, float]:
        u, v = rg.world_to_local(cx, cy, copper.angle_deg, x, y)
        return (u, v) if sy > sx else (-v, u)

    toward = local(*other_center)[1]
    if abs(toward) < pad_exits.EPS:
        raise LayFailure("pad_exit_failed", f"{pad.name}: neither short edge faces the other pad")
    sign = 1.0 if toward > 0 else -1.0

    def pos(pt: tuple[float, float]) -> tuple[float, float]:
        lx, ly = local(pt[0], pt[1])
        return sign * lx, sign * ly

    w, h = sorted((sx / 2.0, sy / 2.0))
    margin = max(w, h) * 1.75
    local_segs = []
    for seg in layer_segments:
        if seg.layer not in pad.layers:
            continue
        pa, pb = pos(seg.start), pos(seg.end)
        lim = margin + seg.width / 2.0
        if any(min(pa[k], pb[k]) > lim or max(pa[k], pb[k]) < -lim for k in (0, 1)):
            continue
        local_segs.append((pa, pb, seg.width))
    report = pad_exits.crossing_report(w, h, local_segs, radius)
    if report["issues"]:
        raise LayFailure("pad_exit_failed", f"{pad.name}: {'; '.join(report['issues'])}",
                         pad=pad.name, issues=report["issues"], crossings=report["crossings"])


def _is_finite_number(val) -> bool:
    return isinstance(val, (int, float)) and not isinstance(val, bool) and math.isfinite(float(val))


def _point(raw, label: str = "point") -> tuple[float, float]:
    if not isinstance(raw, (list, tuple)) or len(raw) != 2 or not (_is_finite_number(raw[0]) and _is_finite_number(raw[1])):
        raise LayFailure("bad_request", f"{label} must be [x, y] finite coordinates, got {raw!r}")
    return snap_pt((float(raw[0]), float(raw[1])))


def _step_width(raw_w, default: float) -> float:
    if raw_w is None:
        return default
    if not _is_finite_number(raw_w) or float(raw_w) <= 0:
        raise LayFailure("bad_request", f"invalid step track width {raw_w!r}")
    return float(raw_w)


def _normalize_step(raw, current_layer: str, current_width: float) -> tuple[str, tuple[float, float], str, float]:
    """Normalize a JSON/CLI step into (kind, point_mm, next_layer, next_width)."""
    if isinstance(raw, (list, tuple)):
        return ("through", _point(raw, "step point"), current_layer, current_width)
    if not isinstance(raw, dict):
        raise LayFailure("bad_request", f"invalid step {raw!r}: expected [x, y] or {{through|point|via: [x, y]}}")
    if "via" in raw:
        pt = _point(raw["via"], "via coordinates")
        next_layer = str(raw.get("layer") or raw.get("to_layer") or kr.OTHER_SIDE.get(current_layer, current_layer))
        return ("via", pt, next_layer, _step_width(raw.get("width"), current_width))
    for key in ("through", "point"):
        if key in raw:
            pt = _point(raw[key], f"{key} coordinates")
            next_layer = str(raw.get("layer") or current_layer)
            return ("through", pt, next_layer, _step_width(raw.get("width"), current_width))
    raise LayFailure("bad_request", f"invalid step {raw!r}: missing through, point or via")


def _valid_pad_ref(val) -> bool:
    if not isinstance(val, str) or not val.strip():
        return False
    base = val.partition("@")[0].strip(".")
    return "." in base and bool(base.rpartition(".")[2])


def _validate_route_endpoints(route: dict) -> tuple[bool, bool, bool]:
    from_val = route.get("from")
    if isinstance(from_val, (list, tuple)):
        raise LayFailure(
            "bad_request",
            f"route 'from' must be a 'REF.PAD' pad string, got {from_val!r}; "
            "to tap into an existing track or via, route from a pad with '\"from\": \"REF.PAD\", \"to_point\": [x, y]' instead",
        )
    if not _valid_pad_ref(from_val):
        raise LayFailure("bad_request", f"route 'from' must be a 'REF.PAD' string, got {from_val!r}")
    to_raw = route.get("to")
    has_to = to_raw is not None and to_raw != ""
    has_to_via = route.get("to_via") is not None
    has_to_point = route.get("to_point") is not None
    target_count = int(has_to) + int(has_to_via) + int(has_to_point)
    if target_count > 1:
        raise LayFailure("bad_request", "route must specify only one of 'to', 'to_via', or 'to_point'")
    if target_count == 0:
        raise LayFailure("bad_request", "every route requires 'from' and one of 'to' ('REF.PAD'), 'to_via' ([x, y]), or 'to_point' ([x, y])")
    if has_to:
        if isinstance(to_raw, (list, tuple)):
            raise LayFailure(
                "bad_request",
                f"route 'to' must be a 'REF.PAD' pad string, got {to_raw!r}; "
                "use '\"to_via\": [x, y]' for a pour stitching via or '\"to_point\": [x, y]' to tap an existing track or via",
            )
        if not _valid_pad_ref(to_raw):
            raise LayFailure("bad_request", f"route 'to' must be a 'REF.PAD' string, got {to_raw!r}")
    if has_to_via:
        _point(route["to_via"], "to_via")
        for key in ("pour_layer", "target_layer"):
            val = route.get(key)
            if val is not None and (not isinstance(val, str) or not val.strip()):
                raise LayFailure("bad_request", f"route '{key}' must be a non-empty layer string, got {val!r}")
    if has_to_point:
        _point(route["to_point"], "to_point")
        for key in ("end_layer", "target_layer"):
            val = route.get(key)
            if val is not None and (not isinstance(val, str) or not val.strip()):
                raise LayFailure("bad_request", f"route '{key}' must be a non-empty layer string, got {val!r}")
    return has_to, has_to_via, has_to_point


def _prevalidate_route_spec(route, idx: int) -> None:
    if not isinstance(route, dict):
        raise LayFailure("bad_request", f"route[{idx}] must be an object, got {route!r}")
    _validate_route_endpoints(route)
    steps = route.get("steps")
    if steps is not None:
        if not isinstance(steps, list):
            raise LayFailure("bad_request", f"route 'steps' must be a list, got {type(steps).__name__}")
        for raw_step in steps:
            _normalize_step(raw_step, "F.Cu", 0.25)


def _prevalidate_batch_spec(spec: dict) -> None:
    """Fail-fast pure schema validation so malformed batch specs reject without touching KiCad IPC."""
    if not isinstance(spec, dict):
        raise LayFailure("bad_request", "batch JSON must be an object")
    raw_routes = spec.get("routes")
    raw_pours = spec.get("pours")
    raw_vias = spec.get("vias")
    for key, val in (("routes", raw_routes), ("pours", raw_pours), ("vias", raw_vias)):
        if val is not None and not isinstance(val, list):
            raise LayFailure("bad_request", f"'{key}' must be a list")
    routes = raw_routes or []
    pours = raw_pours or []
    vias = raw_vias or []
    if not routes and not pours and not vias:
        raise LayFailure("bad_request", "batch requires at least one non-empty 'routes', 'pours' or 'vias' list")
    for idx, route in enumerate(routes):
        _prevalidate_route_spec(route, idx)
    default_net = spec.get("net")
    for via in vias:
        if not isinstance(via, dict):
            raise LayFailure("bad_request", f"via spec must be an object, got {via!r}")
        if not str(via.get("net") or default_net or "").strip():
            raise LayFailure("bad_request", "every standalone via requires 'net'")
        if "at" in via:
            _point(via["at"], "via 'at'")
        elif "x" in via and "y" in via:
            _point([via["x"], via["y"]], "via x/y")
        else:
            raise LayFailure("bad_request", "every standalone via requires 'at': [x, y]")
    for pour in pours:
        if not isinstance(pour, dict):
            raise LayFailure("bad_request", f"pour spec must be an object, got {pour!r}")
        if not str(pour.get("net") or default_net or "").strip():
            raise LayFailure("bad_request", "every pour requires 'net'")
        has_layer = any(pour.get(k) is not None for k in ("layer", "layers")) or any(
            spec.get(k) is not None for k in ("layer", "layers")
        )
        if not has_layer:
            raise LayFailure("bad_request", "every pour requires at least one copper layer")


def _plan_route_geometry(
    pad_a: kr.PadInfo,
    target_pt: tuple[float, float],
    target_pad: kr.PadInfo | None,
    net_name: str,
    start_layer: str,
    enabled: set[str],
    width: float,
    via_dia: float,
    via_drill: float,
    centered: bool,
    escape_mm: float,
    raw_steps: list,
    terminal_via: bool,
) -> tuple[list[PlannedSegment], list[PlannedVia], str, float]:
    a_pt = snap_pt(pad_a.copper.center if pad_a.copper else (pad_a.x, pad_a.y))
    cur_layer, cur_width = start_layer, width
    normalized_steps = []
    for raw_step in raw_steps:
        kind, pt, next_layer, next_width = _normalize_step(raw_step, cur_layer, cur_width)
        if next_layer not in enabled:
            raise LayFailure("bad_request", f"layer {next_layer!r} is not enabled on this board")
        if next_width <= 0:
            raise LayFailure("bad_request", f"invalid step track width {next_width}")
        normalized_steps.append((kind, cur_layer, cur_width, pt, next_layer, next_width))
        cur_layer, cur_width = next_layer, next_width

    end_layer = cur_layer
    if target_pad is not None and end_layer not in target_pad.layers:
        raise LayFailure("bad_request",
                         f"{target_pad.name} is on {target_pad.layers}, cannot end route on {end_layer} (add a via step)")

    def on_ray(origin, unit, half_long, pt) -> bool:
        vx, vy = pt[0] - origin[0], pt[1] - origin[1]
        along = vx * unit[0] + vy * unit[1]
        perp = abs(vx * unit[1] - vy * unit[0])
        return along > half_long + 0.02 and perp <= 0.001

    def _resolve_escape(pad: kr.PadInfo, default_ref: tuple[float, float], step_pt: tuple[float, float] | None):
        try:
            return facing_escape(pad, default_ref, escape_mm), default_ref
        except LayFailure as exc:
            if "neither short edge faces" not in exc.reason:
                raise
            if step_pt is None:
                raise LayFailure(
                    "bad_request",
                    f"{pad.name}: neither short edge faces ({default_ref[0]:.3f}, {default_ref[1]:.3f}); "
                    "add a waypoint in 'steps' leaving the desired short edge or set \"centered_exits\": false",
                ) from None
            try:
                return facing_escape(pad, step_pt, escape_mm), step_pt
            except LayFailure as exc2:
                if "neither short edge faces" not in exc2.reason:
                    raise
                raise LayFailure(
                    "bad_request",
                    f"{pad.name}: neither short edge faces ({default_ref[0]:.3f}, {default_ref[1]:.3f}) "
                    f"or step ({step_pt[0]:.3f}, {step_pt[1]:.3f}); "
                    "place the adjacent 'steps' waypoint off the pad's short edge or set \"centered_exits\": false",
                ) from None

    start_dir = end_in_dir = None
    ref_a, ref_b = target_pt, a_pt
    start_anchor, end_anchor = a_pt, target_pt
    prefix_segs: list[PlannedSegment] = []
    suffix_segs: list[PlannedSegment] = []

    if centered:
        first_step = normalized_steps[0][3] if normalized_steps else None
        last_step = normalized_steps[-1][3] if normalized_steps else None
        esc_a, ref_a = _resolve_escape(pad_a, target_pt, first_step)
        if esc_a["compass"] is None:
            raise LayFailure("pad_exit_failed", f"centered-exit axis for {pad_a.name} is not 45-degree aligned with the board")
        start_dir = esc_a["compass"]
        if not (normalized_steps and on_ray(a_pt, esc_a["unit"], esc_a["half_long_mm"], normalized_steps[0][3])):
            if not same_pt(a_pt, esc_a["escape"]):
                prefix_segs.append(PlannedSegment(a_pt, esc_a["escape"], start_layer, width, net_name))
                start_anchor = esc_a["escape"]
        if target_pad is not None:
            esc_b, ref_b = _resolve_escape(target_pad, a_pt, last_step)
            if esc_b["compass"] is None:
                raise LayFailure("pad_exit_failed",
                                 f"centered-exit axis for {pad_a.name}/{target_pad.name} is not 45-degree aligned with the board")
            end_in_dir = (-esc_b["compass"][0], -esc_b["compass"][1])
            if not (normalized_steps and on_ray(target_pt, esc_b["unit"], esc_b["half_long_mm"], normalized_steps[-1][3])):
                if not same_pt(esc_b["escape"], target_pt):
                    suffix_segs.append(PlannedSegment(esc_b["escape"], target_pt, end_layer, cur_width, net_name))
                    end_anchor = esc_b["escape"]

    hops: list[tuple[tuple[float, float], tuple[float, float], str, float]] = []
    vias: list[PlannedVia] = []
    prev_pt, prev_layer, prev_width = start_anchor, start_layer, width
    for kind, _old_layer, _old_w, pt, next_layer, next_w in normalized_steps:
        if kind == "via":
            vias.append(PlannedVia(pt, via_dia, via_drill, net_name))
        if not same_pt(prev_pt, pt):
            hops.append((prev_pt, pt, prev_layer, prev_width))
        elif kind != "via" and prev_layer != next_layer:
            raise LayFailure("bad_request", f"layer change at {pt} without a via step")
        if prev_layer != next_layer and kind != "via":
            raise LayFailure("bad_request", f"layer change from {prev_layer} to {next_layer} at {pt} requires a via step")
        prev_pt, prev_layer, prev_width = pt, next_layer, next_w
    if not same_pt(prev_pt, end_anchor):
        hops.append((prev_pt, end_anchor, prev_layer, prev_width))
    if terminal_via and not any(same_pt(v.at, target_pt) for v in vias):
        vias.append(PlannedVia(target_pt, via_dia, via_drill, net_name))

    raw_segs: list[PlannedSegment] = list(prefix_segs)
    cur_prev_dir = start_dir
    for idx, (h0, h1, h_layer, h_width) in enumerate(hops):
        h_next_dir = end_in_dir if (idx == len(hops) - 1 and end_in_dir is not None) else None
        for s0, s1 in expand_45_leg(h0, h1, cur_prev_dir, h_next_dir):
            raw_segs.append(PlannedSegment(s0, s1, h_layer, h_width, net_name))
            cur_prev_dir = compass_direction(s0, s1)
    raw_segs.extend(suffix_segs)
    segments = merge_collinear(raw_segs)
    if not segments and not vias:
        target_label = target_pad.name if target_pad is not None else str(list(target_pt))
        raise LayFailure("bad_request", f"{pad_a.name} and {target_label} are at the same point")

    if centered:
        verify_centered_exits(pad_a, ref_a, [s for s in segments if s.layer == start_layer], vias)
        if target_pad is not None:
            verify_centered_exits(target_pad, ref_b, [s for s in segments if s.layer == end_layer], vias)
    return segments, vias, end_layer, cur_width


def plan_route(spec: dict, board, defaults: dict | None = None) -> dict:
    """Resolve pads (or `to_via`/`to_point` coordinates), build 45-degree geometry, and run pre-commit geometry checks."""
    defaults = defaults or {}
    has_to, has_to_via, has_to_point = _validate_route_endpoints(spec)
    from_spec = str(spec["from"])
    to_raw = spec.get("to")

    pad_a = board.pad(from_spec)
    net = spec.get("net") or defaults.get("net")
    if not pad_a.net:
        raise LayFailure("bad_request", f"{pad_a.name} has no net assigned")

    enabled = set(board.enabled_layers())
    start_layer = str(spec.get("layer") or defaults.get("layer") or pad_a.layers[0])
    if start_layer not in enabled:
        raise LayFailure("bad_request", f"layer {start_layer!r} is not enabled on this board")
    if start_layer not in pad_a.layers:
        raise LayFailure("bad_request", f"{pad_a.name} is only on {pad_a.layers}, cannot start on {start_layer}")

    nc = board.netclass_defaults(pad_a.net)
    width = float(spec.get("width") or defaults.get("width") or nc["track_width_mm"])
    via_dia = float(spec.get("via_diameter") or defaults.get("via_diameter") or nc["via_diameter_mm"])
    via_drill = float(spec.get("via_drill") or defaults.get("via_drill") or nc["via_drill_mm"])
    if width <= 0 or via_dia <= 0 or via_drill <= 0 or via_drill >= via_dia:
        raise LayFailure("bad_request", f"invalid track width ({width}) or via dimensions ({via_dia}/{via_drill})")

    centered = bool(spec["centered_exits"] if "centered_exits" in spec else defaults.get("centered_exits", False))
    escape_mm = float(spec["escape_mm"] if "escape_mm" in spec else defaults.get("escape_mm", DEFAULT_ESCAPE_MM))
    raw_steps = spec.get("steps") or []

    if has_to_via:
        if net and pad_a.net != net:
            raise LayFailure("wrong_net", f"{pad_a.name} is on {pad_a.net!r}, expected {net!r}")
        v_pt = _point(spec["to_via"], "to_via")
        pour_layer = spec.get("pour_layer") or spec.get("target_layer")
        if pour_layer is not None:
            pour_layer = str(pour_layer).strip()
            if pour_layer not in enabled:
                raise LayFailure("bad_request", f"pour_layer {pour_layer!r} is not enabled on this board")
        segments, vias, _end_layer, _end_width = _plan_route_geometry(
            pad_a, v_pt, None, pad_a.net, start_layer, enabled,
            width, via_dia, via_drill, centered, escape_mm, raw_steps, terminal_via=True,
        )
        return {
            "kind": "to_via",
            "from": pad_a.name,
            "to": f"via@{v_pt[0]:.3f},{v_pt[1]:.3f}",
            "to_via": v_pt,
            "pour_layer": pour_layer,
            "via_diameter": via_dia,
            "net": pad_a.net,
            "pad_a": pad_a,
            "pad_b": None,
            "segments": segments,
            "vias": vias,
        }

    if has_to_point:
        if net and pad_a.net != net:
            raise LayFailure("wrong_net", f"{pad_a.name} is on {pad_a.net!r}, expected {net!r}")
        t_pt = _point(spec["to_point"], "to_point")
        req_end_layer = spec.get("end_layer") or spec.get("target_layer")
        if req_end_layer is not None:
            req_end_layer = str(req_end_layer).strip()
            if req_end_layer not in enabled:
                raise LayFailure("bad_request", f"end_layer {req_end_layer!r} is not enabled on this board")
        segments, vias, end_layer, end_width = _plan_route_geometry(
            pad_a, t_pt, None, pad_a.net, start_layer, enabled,
            width, via_dia, via_drill, centered, escape_mm, raw_steps, terminal_via=False,
        )
        if req_end_layer is not None and end_layer != req_end_layer:
            raise LayFailure(
                "bad_request",
                f"route ends on {end_layer}, which does not match requested target layer {req_end_layer!r}",
            )
        return {
            "kind": "to_point",
            "from": pad_a.name,
            "to": f"pt@{t_pt[0]:.3f},{t_pt[1]:.3f}",
            "to_point": t_pt,
            "end_layer": end_layer,
            "end_width": end_width,
            "net": pad_a.net,
            "pad_a": pad_a,
            "pad_b": None,
            "segments": segments,
            "vias": vias,
        }

    pad_b = board.pad(str(to_raw))
    if not pad_b.net:
        raise LayFailure("bad_request", f"{pad_b.name} has no net assigned")
    if pad_a.net != pad_b.net:
        raise LayFailure("bad_request", f"{pad_a.name} is on {pad_a.net!r}, {pad_b.name} is on {pad_b.net!r}")
    if net and pad_a.net != net:
        raise LayFailure("wrong_net", f"{pad_a.name} is on {pad_a.net!r}, expected {net!r}")
    b_pt = snap_pt(pad_b.copper.center if pad_b.copper else (pad_b.x, pad_b.y))
    segments, vias, _end_layer, _end_width = _plan_route_geometry(
        pad_a, b_pt, pad_b, pad_a.net, start_layer, enabled,
        width, via_dia, via_drill, centered, escape_mm, raw_steps, terminal_via=False,
    )
    return {
        "kind": "pad_to_pad",
        "from": pad_a.name,
        "to": pad_b.name,
        "net": pad_a.net,
        "pad_a": pad_a,
        "pad_b": pad_b,
        "segments": segments,
        "vias": vias,
    }


def plan_via(spec: dict, board, defaults: dict | None = None) -> PlannedVia:
    """Validate and construct one standalone stitching PlannedVia."""
    defaults = defaults or {}
    if not isinstance(spec, dict):
        raise LayFailure("bad_request", f"via spec must be an object, got {spec!r}")
    net = str(spec.get("net") or defaults.get("net") or "")
    if not net:
        raise LayFailure("bad_request", "every standalone via requires 'net'")
    if hasattr(board, "nets") and net not in board.nets():
        raise LayFailure("bad_request", f"unknown net {net!r} on board")
    if "at" in spec:
        pt = spec["at"]
        if not isinstance(pt, (list, tuple)) or len(pt) != 2:
            raise LayFailure("bad_request", f"invalid via 'at': {pt!r}")
        x, y = float(pt[0]), float(pt[1])
    elif "x" in spec and "y" in spec:
        x, y = float(spec["x"]), float(spec["y"])
    else:
        raise LayFailure("bad_request", "every standalone via requires 'at': [x, y]")
    if not (math.isfinite(x) and math.isfinite(y)):
        raise LayFailure("bad_request", f"non-finite via coordinate ({x}, {y})")
    nc = board.netclass_defaults(net)
    dia = float(spec.get("diameter") or defaults.get("via_diameter") or nc["via_diameter_mm"])
    drill = float(spec.get("drill") or defaults.get("via_drill") or nc["via_drill_mm"])
    if dia <= 0 or drill <= 0 or drill >= dia:
        raise LayFailure("bad_request", f"invalid via dimensions ({dia}/{drill})")
    return PlannedVia(snap_pt((x, y)), dia, drill, net)


def _pour_spoke_dim(raw, label: str, connect: str, default_mm: float) -> float | None:
    if raw is None:
        return default_mm if connect in ("thermal", "pth_thermal") else None
    val = float(raw)
    if not math.isfinite(val) or val <= 0:
        raise LayFailure("bad_request", f"{label} must be > 0, got {val!r}")
    return val


def plan_pour(spec: dict, board, defaults: dict | None = None) -> PlannedPour:
    """Validate geometry, net, layers, rules and optional replacement targets for one copper zone."""
    defaults = defaults or {}
    if not isinstance(spec, dict):
        raise LayFailure("bad_request", f"pour spec must be an object, got {spec!r}")
    net_name = str(spec.get("net") or defaults.get("net") or "")
    if not net_name:
        raise LayFailure("bad_request", "every pour requires 'net'")
    if net_name not in board.nets():
        raise LayFailure("bad_request", f"unknown net {net_name!r} on board")

    raw_layers = spec.get("layers")
    raw_layer = spec.get("layer")
    if raw_layers is not None and raw_layer is not None:
        raise LayFailure("bad_request", "specify either 'layer' or 'layers' on a pour, not both")
    if raw_layers is None and raw_layer is None:
        raw_layers = defaults.get("layers") if defaults.get("layers") is not None else defaults.get("layer")
    if isinstance(raw_layers, str):
        layers_list = [p.strip() for p in raw_layers.split(",") if p.strip()]
    elif isinstance(raw_layers, (list, tuple)):
        layers_list = [str(p).strip() for p in raw_layers if str(p).strip()]
    elif raw_layer is not None:
        layers_list = [str(raw_layer).strip()]
    else:
        layers_list = []
    layers_list = list(dict.fromkeys(layers_list))
    if not layers_list:
        raise LayFailure("bad_request", "every pour requires at least one copper layer")
    enabled = set(board.enabled_layers())
    for layer in layers_list:
        if layer not in enabled:
            raise LayFailure("bad_request", f"layer {layer!r} is not enabled on this board")

    geo_keys = [k for k in ("outline", "board_outline", "rect", "polygon") if spec.get(k) not in (None, False)]
    if len(geo_keys) != 1:
        raise LayFailure("bad_request", "specify exactly one pour geometry: outline='board', rect=[x0,y0,x1,y1], or polygon=[[x,y],...]")
    key = geo_keys[0]
    if key in ("outline", "board_outline"):
        if key == "outline" and spec["outline"] != "board":
            raise LayFailure("bad_request", f"unsupported outline mode {spec['outline']!r}; expected 'board'")
        raw_outline = board.board_outline()["polygon_mm"]
    elif key == "rect":
        rect = spec["rect"]
        if not isinstance(rect, (list, tuple)) or len(rect) != 4:
            raise LayFailure("bad_request", f"rect requires [x0, y0, x1, y1], got {rect!r}")
        x0, y0, x1, y1 = (float(v) for v in rect)
        if not all(math.isfinite(v) for v in (x0, y0, x1, y1)):
            raise LayFailure("bad_request", f"non-finite rect bounds: {rect!r}")
        lx, hx = min(x0, x1), max(x0, x1)
        ly, hy = min(y0, y1), max(y0, y1)
        if (hx - lx) <= 1e-3 or (hy - ly) <= 1e-3:
            raise LayFailure("bad_geometry", f"rect has degenerate width or height: {rect!r}")
        raw_outline = [(lx, ly), (hx, ly), (hx, hy), (lx, hy)]
    else:
        raw_outline = spec["polygon"]

    inset = float(spec.get("inset", defaults.get("inset", 0.0)))
    outline = inset_polygon(raw_outline, inset)

    holes: list[tuple[tuple[float, float], ...]] = []
    for raw_hole in spec.get("holes") or ():
        holes.append(validate_polygon(raw_hole, "pour hole"))

    name = str(spec.get("name", defaults.get("name", "")) or "")
    priority = spec.get("priority", defaults.get("priority", 0))
    if type(priority) is not int or priority < 0:
        raise LayFailure("bad_request", f"pour priority must be a non-negative integer, got {priority!r}")

    nc = board.netclass_defaults(net_name)
    raw_cl = spec.get("clearance", defaults.get("clearance"))
    clearance = float(raw_cl) if raw_cl is not None else float(nc["clearance_mm"])
    if not math.isfinite(clearance) or clearance <= 0:
        raise LayFailure("bad_request", f"pour clearance must be > 0, got {clearance!r}")

    min_thickness = float(spec.get("min_thickness", defaults.get("min_thickness", DEFAULT_ZONE_MIN_THICKNESS_MM)))
    if not math.isfinite(min_thickness) or min_thickness <= 0:
        raise LayFailure("bad_request", f"pour min_thickness must be > 0, got {min_thickness!r}")

    connect = str(spec.get("connect", spec.get("connect_pads", defaults.get("connect", "thermal")))).lower()
    if connect not in ZONE_CONNECT_MODES:
        raise LayFailure("bad_request", f"unsupported connect mode {connect!r}; use {', '.join(ZONE_CONNECT_MODES)}")

    thermal_gap = _pour_spoke_dim(
        spec.get("thermal_gap", defaults.get("thermal_gap")),
        "thermal_gap", connect, DEFAULT_THERMAL_GAP_MM,
    )
    thermal_bridge = _pour_spoke_dim(
        spec.get("thermal_bridge_width", spec.get("thermal_bridge", defaults.get("thermal_bridge_width"))),
        "thermal_bridge_width", connect, DEFAULT_THERMAL_BRIDGE_WIDTH_MM,
    )

    island_mode = str(spec.get("island_mode", defaults.get("island_mode", "always"))).lower()
    if island_mode not in ZONE_ISLAND_MODES:
        raise LayFailure("bad_request", f"unsupported island_mode {island_mode!r}; use {', '.join(ZONE_ISLAND_MODES)}")

    min_island = float(spec.get("min_island_area", defaults.get("min_island_area", DEFAULT_ZONE_MIN_ISLAND_MM2)))
    if not math.isfinite(min_island) or min_island < 0:
        raise LayFailure("bad_request", f"min_island_area must be >= 0, got {min_island!r}")

    replace = bool(spec.get("replace", defaults.get("replace", False)))
    explicit_ids = [str(zid) for zid in (spec.get("replace_ids") or ())]
    existing_zones = board.zones()
    by_id = {z["id"]: z for z in existing_zones}
    replace_ids: list[str] = []
    for zid in explicit_ids:
        target_z = by_id.get(zid)
        if target_z is None:
            raise LayFailure("not_found", f"zone {zid} not found on board")
        if target_z["type"] == "rule_area":
            raise LayFailure("forbidden", f"zone {zid} is a rule area; rule areas cannot be modified")
        if target_z.get("locked"):
            raise LayFailure("locked", f"zone {zid} is locked and cannot be replaced")
        if zid not in replace_ids:
            replace_ids.append(zid)
    if replace:
        target_layers = set(layers_list)
        for z in existing_zones:
            if z["type"] == "copper" and z["net"] == net_name and set(z["layers"]) == target_layers:
                if z.get("locked"):
                    raise LayFailure("locked", f"existing matching pour {z['id']} is locked and cannot be replaced")
                if z["id"] not in replace_ids:
                    replace_ids.append(z["id"])

    return PlannedPour(
        net=net_name,
        layers=tuple(layers_list),
        outline=outline,
        holes=tuple(holes),
        name=name,
        priority=priority,
        clearance=clearance,
        min_thickness=min_thickness,
        connect=connect,
        thermal_gap=thermal_gap,
        thermal_bridge_width=thermal_bridge,
        island_mode=island_mode,
        min_island_area=min_island,
        replace=replace,
        replace_ids=tuple(replace_ids),
    )


def _drc_error_counts(report: dict) -> tuple[collections.Counter, dict]:
    counts: collections.Counter = collections.Counter()
    samples: dict = {}
    for v in report.get("violations", []):
        # Match editor_step_audit.design_check so laying rolls back on new DRC warnings too (except isolated copper).
        if v.get("severity") not in ("error", "warning") or (
                v.get("severity") == "warning" and v.get("type") == "isolated_copper"):
            continue
        places = tuple(sorted((round(i.get("pos", {}).get("x", 0.0), 4),
                               round(i.get("pos", {}).get("y", 0.0), 4))
                              for i in v.get("items", [])))
        key = (v.get("type"), v.get("description"), places)
        counts[key] += 1
        samples[key] = {
            "type": v.get("type"),
            "description": v.get("description"),
            "at": [list(p) for p in places],
        }
    return counts, samples


def new_drc_errors(before_report: dict, after_report: dict) -> list[dict]:
    before_counts, _ = _drc_error_counts(before_report)
    after_counts, samples = _drc_error_counts(after_report)
    added = after_counts - before_counts
    return [samples[key] for key in added.elements()]
