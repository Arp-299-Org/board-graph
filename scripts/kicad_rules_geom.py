#!/usr/bin/env python3
"""Pure Python KiCad rules, 2D pad/copper geometry, and S-expression canonicalization helpers.

Zero `kipy` or GUI imports; Python 3.9 compatible.
"""
from __future__ import annotations

import fnmatch
import json
import math
import pathlib
import re

import kicad_sexp

NM = 1_000_000
DEFAULT_TRACK_MM = 0.25
DEFAULT_VIA_DIA_MM = 0.6
DEFAULT_VIA_DRILL_MM = 0.3
DEFAULT_CLEARANCE_MM = 0.2
ENDPOINT_EPS_MM = 1e-4

TOP_LEVEL_HEADERS = ("version", "generator", "generator_version")
FOOTPRINT_HEADER = {"version", "generator", "generator_version"}
_NUMERIC_SEXP_HEADS = frozenset({
    "at", "start", "mid", "end", "center", "size", "drill", "width", "thickness",
    "roundrect_rratio", "clearance", "solder_mask_margin", "solder_paste_margin",
    "solder_paste_ratio", "thermal_gap", "thermal_bridge_width", "xyz", "xy",
})
_KICAD10_SETUP_DEFAULTS = (
    ["tenting", ["front", "yes"], ["back", "yes"]],
    ["covering", ["front", "no"], ["back", "no"]],
    ["plugging", ["front", "no"], ["back", "no"]],
    ["capping", "no"],
    ["filling", "no"],
)

_MM_OR_MIL_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*(mm|mil)?$", re.IGNORECASE)


class LayFailure(Exception):
    """Refusal with a machine-readable status code and optional structured details."""

    def __init__(self, status: str, reason: str, **extra):
        super().__init__(reason)
        self.status, self.reason, self.extra = status, reason, extra


def to_nm(mm: float) -> int:
    return int(round(float(mm) * NM))


def to_mm(nm: int) -> float:
    return round(nm / NM, 6)


def snap_pt(pt: tuple[float, float]) -> tuple[float, float]:
    return (to_mm(to_nm(pt[0])), to_mm(to_nm(pt[1])))


def same_pt(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return to_nm(a[0]) == to_nm(b[0]) and to_nm(a[1]) == to_nm(b[1])


def _fmt_num(val: float) -> str:
    rounded = round(float(val), 4)
    if rounded == 0.0:
        rounded = 0.0
    return f"{rounded:.4f}".rstrip("0").rstrip(".") or "0"


def parse_mm(raw) -> float | None:
    """Parse a dimension token supporting mm, mil (* 0.0254), and bare numbers."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        val = float(raw)
        return val if math.isfinite(val) else None
    text = str(raw).strip()
    m_val = _MM_OR_MIL_RE.match(text)
    if not m_val:
        return None
    val = float(m_val.group(1))
    if not math.isfinite(val):
        return None
    unit = (m_val.group(2) or "mm").lower()
    if unit == "mil":
        val *= 0.0254
    return val


def local_to_world(ox: float, oy: float, angle_deg: float, lx: float, ly: float) -> tuple[float, float]:
    """Transform pad/footprint-local coordinates to KiCad Y-down world coordinates (angle CCW in deg)."""
    rad = math.radians(float(angle_deg))
    ca, sa = math.cos(rad), math.sin(rad)
    return (ox + lx * ca + ly * sa, oy - lx * sa + ly * ca)


def world_to_local(ox: float, oy: float, angle_deg: float, wx: float, wy: float) -> tuple[float, float]:
    """Transform KiCad Y-down world coordinates into local coordinates of an item rotated CCW by angle_deg."""
    rad = math.radians(float(angle_deg))
    ca, sa = math.cos(rad), math.sin(rad)
    dx, dy = wx - ox, wy - oy
    return (dx * ca - dy * sa, dx * sa + dy * ca)


def rotated_pad_aabb_half(sx: float, sy: float, angle_deg: float) -> tuple[float, float]:
    """Half-extents (hx, hy) of the axis-aligned bounding box enclosing a rotated rectangle."""
    rad = math.radians(float(angle_deg))
    ca, sa = math.cos(rad), math.sin(rad)
    return ((abs(sx * ca) + abs(sy * sa)) / 2.0, (abs(sx * sa) + abs(sy * ca)) / 2.0)


def pad_corner_radius(
    shape: str,
    sx: float,
    sy: float,
    round_ratio: float | None = None,
    *,
    strict: bool = True,
    clamp_ratio: bool = False,
) -> float | None:
    """Corner radius in mm for standard pad shapes, or None when unsupported/invalid."""
    hx, hy = sx / 2.0, sy / 2.0
    if hx <= 0.0 or hy <= 0.0:
        return None
    kind = str(shape or "rect").lower()
    if kind == "rect":
        return 0.0
    if kind == "oval":
        return min(hx, hy)
    if kind == "circle":
        if strict and abs(hx - hy) > 1e-6:
            return None
        return min(hx, hy)
    if kind == "roundrect":
        rr = float(round_ratio or 0.0)
        if clamp_ratio:
            rr = max(0.0, min(0.5, rr))
        elif not (0.0 <= rr <= 0.5):
            return None
        return min(sx, sy) * rr
    return None


def pad_signed_distance(
    wx: float,
    wy: float,
    cx: float,
    cy: float,
    angle_deg: float,
    shape: str,
    sx: float,
    sy: float,
    round_ratio: float | None = None,
) -> float | None:
    """Signed distance from world point (wx, wy) to pad copper: <= 0 inside, > 0 outside, None if unsupported."""
    hx, hy = sx / 2.0, sy / 2.0
    rc = pad_corner_radius(shape, sx, sy, round_ratio, strict=True, clamp_ratio=False)
    if rc is None:
        return None
    x, y = world_to_local(cx, cy, angle_deg, wx, wy)
    qx, qy = abs(x) - (hx - rc), abs(y) - (hy - rc)
    return math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - rc


def dist_point_to_seg(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    dx, dy = bx - ax, by - ay
    len2 = dx * dx + dy * dy
    if len2 <= 1e-18:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / len2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def segments_cross(
    a: tuple[float, float], b: tuple[float, float], c: tuple[float, float], d: tuple[float, float]
) -> bool:
    def orient(p, q, r) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    def on_seg(p, q, r) -> bool:
        return (
            min(p[0], r[0]) - 1e-6 <= q[0] <= max(p[0], r[0]) + 1e-6
            and min(p[1], r[1]) - 1e-6 <= q[1] <= max(p[1], r[1]) + 1e-6
        )

    o1 = orient(a, b, c)
    o2 = orient(a, b, d)
    o3 = orient(c, d, a)
    o4 = orient(c, d, b)
    eps = 1e-7
    if (o1 > eps and o2 < -eps or o1 < -eps and o2 > eps) and (o3 > eps and o4 < -eps or o3 < -eps and o4 > eps):
        return True
    return (
        (abs(o1) <= eps and on_seg(a, c, b))
        or (abs(o2) <= eps and on_seg(a, d, b))
        or (abs(o3) <= eps and on_seg(c, a, d))
        or (abs(o4) <= eps and on_seg(c, b, d))
    )


def dist_seg_to_seg(
    a0: tuple[float, float], a1: tuple[float, float], b0: tuple[float, float], b1: tuple[float, float]
) -> float:
    if segments_cross(a0, a1, b0, b1):
        return 0.0
    return min(
        dist_point_to_seg(a0[0], a0[1], b0[0], b0[1], b1[0], b1[1]),
        dist_point_to_seg(a1[0], a1[1], b0[0], b0[1], b1[0], b1[1]),
        dist_point_to_seg(b0[0], b0[1], a0[0], a0[1], a1[0], a1[1]),
        dist_point_to_seg(b1[0], b1[1], a0[0], a0[1], a1[0], a1[1]),
    )


def dist_point_to_aabb(px: float, py: float, hx: float, hy: float) -> float:
    return math.hypot(max(0.0, abs(px) - hx), max(0.0, abs(py) - hy))


def dist_seg_to_aabb(p0: tuple[float, float], p1: tuple[float, float], hx: float, hy: float) -> float:
    if hx <= 0.0 and hy <= 0.0:
        return dist_point_to_seg(0.0, 0.0, p0[0], p0[1], p1[0], p1[1])
    if (abs(p0[0]) <= hx and abs(p0[1]) <= hy) or (abs(p1[0]) <= hx and abs(p1[1]) <= hy):
        return 0.0
    corners = ((-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy))
    for i in range(4):
        if segments_cross(p0, p1, corners[i], corners[(i + 1) % 4]):
            return 0.0
    return min(
        dist_point_to_aabb(p0[0], p0[1], hx, hy),
        dist_point_to_aabb(p1[0], p1[1], hx, hy),
        *(dist_point_to_seg(cx, cy, p0[0], p0[1], p1[0], p1[1]) for cx, cy in corners),
    )


def dist_to_pad(kind: str, p0: tuple[float, float], p1: tuple[float, float], pad: dict) -> float:
    """Euclidean distance from a via center or segment centerline to the copper boundary of an oriented pad."""
    cx, cy = pad["center"]
    sx, sy = pad["size"]
    angle_deg = float(pad.get("angle_deg") or 0.0)
    hx, hy = sx / 2.0, sy / 2.0
    shape = str(pad.get("shape") or "rect").lower()
    if shape == "circle":
        rc = min(hx, hy)
        core_hx = core_hy = 0.0
    else:
        rc_val = pad_corner_radius(shape, sx, sy, pad.get("round_ratio"), strict=False, clamp_ratio=True)
        rc = rc_val if rc_val is not None else 0.0
        core_hx, core_hy = max(0.0, hx - rc), max(0.0, hy - rc)

    q0 = world_to_local(cx, cy, angle_deg, p0[0], p0[1])
    if kind == "via":
        core_dist = dist_point_to_aabb(q0[0], q0[1], core_hx, core_hy)
    else:
        q1 = world_to_local(cx, cy, angle_deg, p1[0], p1[1])
        core_dist = dist_seg_to_aabb(q0, q1, core_hx, core_hy)
    return max(0.0, core_dist - rc)


def polygon_signed_area(pts: list[tuple[float, float]] | tuple[tuple[float, float], ...]) -> float:
    """Shoelace signed area (positive when CCW in XY coordinates)."""
    n = len(pts)
    if n < 3:
        return 0.0
    total = 0.0
    for i in range(n):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % n]
        total += x0 * y1 - x1 * y0
    return 0.5 * total


def clean_ring(raw_pts) -> list[tuple[float, float]]:
    cleaned: list[tuple[float, float]] = []
    for raw in raw_pts:
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            raise LayFailure("bad_geometry", f"expected [x, y] vertex, got {raw!r}")
        x, y = float(raw[0]), float(raw[1])
        if not (math.isfinite(x) and math.isfinite(y)):
            raise LayFailure("bad_geometry", f"non-finite polygon vertex: {raw!r}")
        pt = snap_pt((x, y))
        if not cleaned or not same_pt(cleaned[-1], pt):
            cleaned.append(pt)
    if len(cleaned) >= 2 and same_pt(cleaned[0], cleaned[-1]):
        cleaned.pop()
    return cleaned


def validate_polygon(raw_pts, label: str = "polygon") -> tuple[tuple[float, float], ...]:
    """Normalize and validate a simple non-self-intersecting closed polygon ring."""
    pts = clean_ring(raw_pts)
    if len(pts) < 3:
        raise LayFailure("bad_geometry", f"{label} needs at least 3 distinct vertices")
    if abs(polygon_signed_area(pts)) <= 1e-4:
        raise LayFailure("bad_geometry", f"{label} has zero or degenerate area")
    n = len(pts)
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        for j in range(i + 2, n):
            if i == 0 and j == n - 1:
                continue
            c, d = pts[j], pts[(j + 1) % n]
            if segments_cross(a, b, c, d):
                raise LayFailure("bad_geometry", f"{label} edges self-intersect between segments {i} and {j}")
    return tuple(pts)


def point_in_ring(pt: tuple[float, float], ring: list | tuple, eps: float = 1e-4) -> bool:
    """True when `pt` is inside or within `eps` mm of the boundary of closed polygon `ring`."""
    if not ring or len(ring) < 3:
        return False
    px, py = float(pt[0]), float(pt[1])
    inside = False
    n = len(ring)
    eps2 = eps * eps
    for i in range(n):
        ax, ay = float(ring[i][0]), float(ring[i][1])
        bx, by = float(ring[(i + 1) % n][0]), float(ring[(i + 1) % n][1])
        dx, dy = bx - ax, by - ay
        seg_len2 = dx * dx + dy * dy
        if seg_len2 <= 1e-18:
            if (px - ax) * (px - ax) + (py - ay) * (py - ay) <= eps2:
                return True
            continue
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg_len2))
        cx, cy = ax + t * dx, ay + t * dy
        if (px - cx) * (px - cx) + (py - cy) * (py - cy) <= eps2:
            return True
        if (ay > py) != (by > py):
            x_int = ax + (py - ay) * dx / dy
            if px < x_int:
                inside = not inside
    return inside


def point_in_filled_zone(pt: tuple[float, float], polys: list | tuple) -> bool:
    """True when `pt` falls inside at least one filled (outline, holes) polygon and outside its holes."""
    for outline, holes in polys:
        if point_in_ring(pt, outline) and not any(point_in_ring(pt, hole) for hole in holes):
            return True
    return False


def sample_arc_points(
    start: tuple[float, float], mid: tuple[float, float], end: tuple[float, float], max_step_deg: float = 15.0
) -> list[tuple[float, float]]:
    """Sample a 3-point KiCad arc (start, mid, end) into ordered polyline vertices."""
    ax, ay = start
    bx, by = mid
    cx, cy = end
    d = 2.0 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-9:
        return [snap_pt(start), snap_pt(end)]
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    radius = math.hypot(ax - ux, ay - uy)
    a0 = math.atan2(ay - uy, ax - ux)
    am = math.atan2(by - uy, bx - ux)
    a1 = math.atan2(cy - uy, cx - ux)

    def _cw_delta(t0: float, t1: float) -> float:
        return (t1 - t0) % (2.0 * math.pi)

    sweep = _cw_delta(a0, a1)
    mid_delta = _cw_delta(a0, am)
    if mid_delta > sweep:
        sweep -= 2.0 * math.pi
    steps = max(4, int(math.ceil(abs(math.degrees(sweep)) / max_step_deg)))
    pts = [snap_pt(start)]
    for i in range(1, steps):
        ang = a0 + sweep * (i / steps)
        pt = snap_pt((ux + radius * math.cos(ang), uy + radius * math.sin(ang)))
        if not same_pt(pts[-1], pt):
            pts.append(pt)
    if not same_pt(pts[-1], snap_pt(end)):
        pts.append(snap_pt(end))
    return pts


def stitch_outline_edges(chains: list[list[tuple[float, float]]], tol_mm: float = 0.005) -> list[list[tuple[float, float]]]:
    """Stitch unordered open edge chains into closed polygon loops ordered by largest area first."""
    unused = [list(c) for c in chains if len(c) >= 2 and not (len(c) == 2 and same_pt(c[0], c[-1]))]
    loops: list[list[tuple[float, float]]] = []

    def close(p: tuple[float, float], q: tuple[float, float]) -> bool:
        return abs(p[0] - q[0]) <= tol_mm and abs(p[1] - q[1]) <= tol_mm

    while unused:
        ring = list(unused.pop(0))
        progressed = True
        while progressed and not close(ring[0], ring[-1]):
            progressed = False
            tail = ring[-1]
            for idx, seg in enumerate(unused):
                if close(tail, seg[0]):
                    ring.extend(seg[1:])
                    unused.pop(idx)
                    progressed = True
                    break
                if close(tail, seg[-1]):
                    ring.extend(reversed(seg[:-1]))
                    unused.pop(idx)
                    progressed = True
                    break
        if len(ring) >= 4 and close(ring[0], ring[-1]):
            ring.pop()
            if len(ring) >= 3:
                loops.append(ring)
    loops.sort(key=lambda loop: abs(polygon_signed_area(loop)), reverse=True)
    return loops


def axis_aligned_rect_bounds(pts: tuple[tuple[float, float], ...]) -> tuple[float, float, float, float] | None:
    if len(pts) != 4:
        return None
    xs = sorted({pt[0] for pt in pts})
    ys = sorted({pt[1] for pt in pts})
    if len(xs) != 2 or len(ys) != 2:
        return None
    for i in range(4):
        a, b = pts[i], pts[(i + 1) % 4]
        if not (abs(a[0] - b[0]) <= 1e-6 or abs(a[1] - b[1]) <= 1e-6):
            return None
    return xs[0], ys[0], xs[1], ys[1]


def inset_polygon(raw_pts, inset_mm: float) -> tuple[tuple[float, float], ...]:
    """Shrink a simple polygon inward by `inset_mm` mm."""
    pts = validate_polygon(raw_pts, "outline")
    inset = float(inset_mm)
    if not math.isfinite(inset) or inset < 0:
        raise LayFailure("bad_request", f"inset must be >= 0 (got {inset_mm!r})")
    if inset == 0.0:
        return pts
    rect = axis_aligned_rect_bounds(pts)
    if rect is not None:
        x0, y0, x1, y1 = rect
        if (x1 - x0) <= 2.0 * inset + 1e-4 or (y1 - y0) <= 2.0 * inset + 1e-4:
            raise LayFailure("bad_geometry", f"inset {inset} mm collapses board/rect outline")
        corners = tuple(
            snap_pt(
                (
                    x0 + inset if abs(px - x0) < 1e-6 else x1 - inset,
                    y0 + inset if abs(py - y0) < 1e-6 else y1 - inset,
                )
            )
            for px, py in pts
        )
        return validate_polygon(corners, "inset outline")

    flipped = polygon_signed_area(pts) < 0
    ccw = list(reversed(pts)) if flipped else list(pts)
    n = len(ccw)
    lines: list[tuple[tuple[float, float], tuple[float, float], tuple[float, float]]] = []
    for i in range(n):
        a, b = ccw[i], ccw[(i + 1) % n]
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        if length <= 1e-9:
            continue
        nx, ny = -dy / length, dx / length
        lines.append(((a[0] + inset * nx, a[1] + inset * ny), (dx / length, dy / length), (nx, ny)))
    m = len(lines)
    if m < 3:
        raise LayFailure("bad_geometry", "outline has fewer than 3 valid edges")
    out: list[tuple[float, float]] = []
    for i in range(m):
        p1, d1, _n1 = lines[(i - 1) % m]
        p2, d2, _n2 = lines[i]
        det = d1[0] * d2[1] - d1[1] * d2[0]
        if abs(det) <= 1e-6:
            if d1[0] * d2[0] + d1[1] * d2[1] < 0:
                raise LayFailure("bad_geometry", f"inset {inset} mm collapses spike edge")
            out.append(snap_pt(p2))
        else:
            rx, ry = p2[0] - p1[0], p2[1] - p1[1]
            t = (rx * d2[1] - ry * d2[0]) / det
            ix, iy = p1[0] + t * d1[0], p1[1] + t * d1[1]
            out.append(snap_pt((ix, iy)))
    if flipped:
        out.reverse()
    validated = validate_polygon(out, "inset outline")
    if (polygon_signed_area(validated) > 0) != (polygon_signed_area(pts) > 0):
        raise LayFailure("bad_geometry", f"inset {inset} mm inverted polygon")
    return validated


def extract_outline_polygon(shapes, canonical_layer=None) -> dict:
    """Extract Board Edge.Cuts bounding box and closed polygon vertices from KiCad BoardShape objects."""
    all_pts: list[tuple[float, float]] = []
    candidates: list[list[tuple[float, float]]] = []
    chains: list[list[tuple[float, float]]] = []

    def _pt_mm(v) -> tuple[float, float]:
        return snap_pt((to_mm(v.x), to_mm(v.y)))

    for shape in shapes:
        layer_val = getattr(shape, "layer", None)
        if layer_val is not None:
            layer_name = canonical_layer(layer_val) if canonical_layer and isinstance(layer_val, int) else str(layer_val)
            if layer_name not in ("Edge.Cuts", "BL_Edge_Cuts"):
                continue
        if hasattr(shape, "top_left") and hasattr(shape, "bottom_right"):
            tl, br = _pt_mm(shape.top_left), _pt_mm(shape.bottom_right)
            x0, x1 = min(tl[0], br[0]), max(tl[0], br[0])
            y0, y1 = min(tl[1], br[1]), max(tl[1], br[1])
            rect = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
            all_pts.extend(rect)
            candidates.append(rect)
        elif hasattr(shape, "polygons"):
            for poly in shape.polygons:
                ring: list[tuple[float, float]] = []
                for node in poly.outline:
                    if getattr(node, "has_point", False):
                        ring.append(_pt_mm(node.point))
                    elif getattr(node, "has_arc", False):
                        a = node.arc
                        ring.extend(sample_arc_points(_pt_mm(a.start), _pt_mm(a.mid), _pt_mm(a.end)))
                all_pts.extend(ring)
                if len(ring) >= 3:
                    candidates.append(ring)
        elif hasattr(shape, "mid") and hasattr(shape, "start") and hasattr(shape, "end"):
            pts = sample_arc_points(_pt_mm(shape.start), _pt_mm(shape.mid), _pt_mm(shape.end))
            all_pts.extend(pts)
            chains.append(pts)
        elif hasattr(shape, "start") and hasattr(shape, "end"):
            pts = [_pt_mm(shape.start), _pt_mm(shape.end)]
            all_pts.extend(pts)
            chains.append(pts)
        elif hasattr(shape, "center") and hasattr(shape, "radius"):
            cx, cy = _pt_mm(shape.center)
            r = to_mm(shape.radius() if callable(shape.radius) else shape.radius)
            pts = [
                snap_pt((cx + r * math.cos(math.radians(a)), cy + r * math.sin(math.radians(a))))
                for a in range(0, 360, 15)
            ]
            all_pts.extend(pts)
            candidates.append(pts)

    candidates.extend(stitch_outline_edges(chains))
    valid_polys: list[tuple[tuple[float, float], ...]] = []
    for cand in candidates:
        try:
            valid_polys.append(validate_polygon(cand, "board outline"))
        except LayFailure:
            continue
    if valid_polys:
        valid_polys.sort(key=lambda poly: abs(polygon_signed_area(poly)), reverse=True)
        best = valid_polys[0]
        xs = [p[0] for p in all_pts or best]
        ys = [p[1] for p in all_pts or best]
        return {"bbox_mm": [min(xs), min(ys), max(xs), max(ys)], "polygon_mm": [list(p) for p in best]}
    if all_pts:
        xs = [p[0] for p in all_pts]
        ys = [p[1] for p in all_pts]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        if (x1 - x0) > 1e-3 and (y1 - y0) > 1e-3:
            return {"bbox_mm": [x0, y0, x1, y1], "polygon_mm": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]}
    raise LayFailure("bad_geometry", "board has no Edge.Cuts outline")


def load_project_rules(
    pro_path: pathlib.Path | None = None,
    *,
    pro_text: str | None = None,
) -> dict:
    """Parse `.kicad_pro` netclasses, netclass assignments/patterns, and board design rules."""
    if pro_text is None:
        if pro_path is None or not pro_path.is_file():
            return {}
        try:
            pro_text = pro_path.read_text(encoding="utf-8")
        except OSError:
            return {}
    try:
        data = json.loads(pro_text)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    ns = data.get("net_settings") or {}
    classes: list[dict] = []
    default_cls: dict | None = None
    for cls in ns.get("classes") or []:
        if not isinstance(cls, dict):
            continue
        entry = {
            "name": str(cls.get("name") or ""),
            "track_width": cls.get("track_width"),
            "via_diameter": cls.get("via_diameter"),
            "via_drill": cls.get("via_drill"),
            "clearance": cls.get("clearance"),
        }
        classes.append(entry)
        if entry["name"] == "Default" and default_cls is None:
            default_cls = entry
    assignments = ns.get("netclass_assignments") or {}
    patterns = ns.get("netclass_patterns") or []
    raw_rules = ((data.get("board") or {}).get("design_settings") or {}).get("rules") or {}
    rule_keys = (
        "min_track_width",
        "min_clearance",
        "min_via_diameter",
        "min_through_hole_diameter",
        "min_copper_edge_clearance",
    )
    design_rules = {
        k: raw_rules[k] for k in rule_keys if k in raw_rules and raw_rules[k] not in (None, 0, 0.0, False)
    }
    if not any((classes, assignments, patterns, design_rules)):
        return {}
    return {
        "classes": classes,
        "default": default_cls,
        "assignments": assignments,
        "patterns": patterns,
        "design_rules": design_rules,
    }


def resolve_netclass(
    project_rules: dict,
    net_name: str,
    *,
    default_clearance: float = DEFAULT_CLEARANCE_MM,
) -> dict:
    """Resolve effective netclass name, track_width, via_diameter, via_drill, and clearance for `net_name`."""
    default_cls = project_rules.get("default")
    if default_cls is None:
        for cls in project_rules.get("classes") or ():
            if cls.get("name") == "Default":
                default_cls = cls
                break

    def _positive(val, fallback: float) -> float:
        try:
            f = float(val)
            return f if math.isfinite(f) and f > 0 else fallback
        except (TypeError, ValueError):
            return fallback

    track_w = _positive(default_cls.get("track_width") if default_cls else None, DEFAULT_TRACK_MM)
    via_dia = _positive(default_cls.get("via_diameter") if default_cls else None, DEFAULT_VIA_DIA_MM)
    via_drill = _positive(default_cls.get("via_drill") if default_cls else None, DEFAULT_VIA_DRILL_MM)
    clearance = _positive(default_cls.get("clearance") if default_cls else None, default_clearance)

    cls_name = "Default"
    if net_name:
        assignments = project_rules.get("assignments") or {}
        assigned = assignments.get(net_name)
        if not assigned:
            for pat_entry in project_rules.get("patterns") or ():
                if not isinstance(pat_entry, dict):
                    continue
                pat = str(pat_entry.get("pattern") or "")
                nc_target = str(pat_entry.get("netclass") or "")
                if pat and nc_target and fnmatch.fnmatchcase(net_name, pat):
                    assigned = nc_target
                    break
        if assigned:
            cls_name = str(assigned)
            for cls in project_rules.get("classes") or ():
                if cls.get("name") == cls_name:
                    track_w = _positive(cls.get("track_width"), track_w)
                    via_dia = _positive(cls.get("via_diameter"), via_dia)
                    via_drill = _positive(cls.get("via_drill"), via_drill)
                    clearance = _positive(cls.get("clearance"), clearance)
                    break

    return {
        "name": cls_name,
        "track_width": track_w,
        "via_diameter": via_dia,
        "via_drill": via_drill,
        "clearance": clearance,
    }


def parse_dru_rules(dru_text_or_path: str | pathlib.Path) -> list[dict]:
    """Parse `.kicad_dru` rules into structured dicts with mm-normalized constraints."""
    if isinstance(dru_text_or_path, pathlib.Path):
        if not dru_text_or_path.is_file():
            return []
        try:
            dru_text = dru_text_or_path.read_text(encoding="utf-8")
        except OSError:
            return []
    else:
        dru_text = str(dru_text_or_path or "")
    raw_lines = [line for line in dru_text.splitlines() if not line.lstrip().startswith("#")]
    body = "\n".join(raw_lines).strip()
    if not body:
        return []
    try:
        parsed = kicad_sexp.parse(f"(dru {body})")
    except ValueError:
        return []
    rules: list[dict] = []
    for rule in kicad_sexp.children(parsed, "rule"):
        rule_name = str(rule[1]) if len(rule) >= 2 and isinstance(rule[1], str) else ""
        cond_node = kicad_sexp.child(rule, "condition")
        cond = str(cond_node[1]) if cond_node and len(cond_node) >= 2 else ""
        constraints: list[dict] = []
        for cons in kicad_sexp.children(rule, "constraint"):
            if len(cons) < 3:
                continue
            kind = str(cons[1])
            bounds: dict[str, float | None] = {}
            for key in ("min", "opt", "max"):
                bound_node = kicad_sexp.child(cons[2:], key)
                bounds[f"{key}_mm"] = parse_mm(bound_node[1]) if bound_node and len(bound_node) >= 2 else None
            constraints.append({"kind": kind, **bounds})
        rules.append({"name": rule_name, "condition": cond, "constraints": constraints})
    return rules


def dru_min_clearance_mm(dru_text_or_path: str | pathlib.Path) -> float | None:
    """Smallest positive `(constraint clearance (min <val>))` in a `.kicad_dru` file, or None."""
    found: list[float] = []
    for rule in parse_dru_rules(dru_text_or_path):
        for cons in rule["constraints"]:
            if cons["kind"] == "clearance" and cons["min_mm"] is not None and cons["min_mm"] > 0:
                found.append(float(cons["min_mm"]))
    return min(found) if found else None


def board_net_map(parsed_board) -> dict[str, str]:
    """Return `{net_id_str: net_name}` from top-level `(net ID "NAME")` entries."""
    return {
        str(n[1]): str(n[2])
        for n in kicad_sexp.children(parsed_board, "net")
        if len(n) >= 3
    }


def resolve_item_net(node, net_map: dict[str, str]) -> str:
    """Resolve an S-expression item's net name across `(net_name "...")`, `(net ID "...")`, `(net ID)`, and `(net "...")`."""
    if not isinstance(node, list):
        return ""
    nn = kicad_sexp.child(node, "net_name")
    if nn and len(nn) >= 2 and str(nn[1]):
        return str(nn[1])
    net_node = kicad_sexp.child(node, "net")
    if not net_node or len(net_node) < 2:
        return ""
    if len(net_node) >= 3:
        return str(net_node[2])
    token = str(net_node[1])
    if token in net_map:
        return net_map[token]
    return "" if token == "0" else token


def expand_copper_layers(tokens: list[str], board_copper: list[str]) -> tuple[str, ...]:
    """Expand KiCad copper layer wildcards (`*.Cu`, `F&B.Cu`) against `board_copper`."""
    out: list[str] = []
    seen: set[str] = set()
    copper_list = list(board_copper) if board_copper else ["F.Cu", "B.Cu"]
    copper_set = set(copper_list) if board_copper else set()

    def _add(layer: str) -> None:
        if layer not in seen:
            seen.add(layer)
            out.append(layer)

    for raw in tokens:
        tok = str(raw)
        if tok == "*.Cu":
            for layer in copper_list:
                _add(layer)
        elif tok == "F&B.Cu":
            matched = [name for name in ("F.Cu", "B.Cu") if not copper_set or name in copper_set]
            for layer in matched or ["F.Cu", "B.Cu"]:
                _add(layer)
        elif (tok in copper_set) if copper_set else tok.endswith(".Cu"):
            _add(tok)
    return tuple(out)


def is_copper_pour_node(node) -> bool:
    """True for top-level non-keepout KiCad copper zones on `.Cu` layers."""
    if not (isinstance(node, list) and node and node[0] == "zone" and not kicad_sexp.child(node, "keepout")):
        return False
    layers_node = kicad_sexp.child(node, "layers") or kicad_sexp.child(node, "layer")
    if not layers_node or len(layers_node) < 2:
        return False
    tokens = [tok for tok in layers_node[1:] if isinstance(tok, str)]
    return bool(tokens) and all(tok.endswith(".Cu") or tok == "*.Cu" or tok == "F&B.Cu" for tok in tokens)


def canonical_float_atom(raw) -> str | float:
    """Normalize numeric strings like '1.000000' and '1' to rounded 4-decimal float if finite."""
    if isinstance(raw, str):
        try:
            val = float(raw)
        except ValueError:
            return raw
        if math.isfinite(val):
            rounded = round(val, 4)
            return 0.0 if rounded == 0.0 else rounded
        return raw
    return raw


def canonical_point(node, tag: str) -> tuple[float, float] | None:
    found = kicad_sexp.child(node, tag)
    if not found or len(found) < 3:
        return None
    try:
        x = round(float(found[1]), 4)
        y = round(float(found[2]), 4)
    except (TypeError, ValueError):
        return None
    return (0.0 if x == 0.0 else x, 0.0 if y == 0.0 else y)


def frozen_sexp(item):
    """Convert nested S-expression lists into immutable hashable tuples."""
    return tuple(frozen_sexp(part) for part in item) if isinstance(item, list) else item


def skip_guard_top_level(item) -> bool:
    """True for non-semantic top-level board declarations safe to ignore in item diffs."""
    return isinstance(item, list) and bool(item) and (
        item[0] in TOP_LEVEL_HEADERS or item[0] == "net" or item == ["embedded_fonts", "no"]
    )


def canonical_num_token(raw):
    """Canonicalize a numeric string token so KiCad 9 '3.0' and KiCad 10 '3' compare equal."""
    if not isinstance(raw, str):
        return raw
    try:
        val = float(raw)
    except ValueError:
        return raw
    if not math.isfinite(val):
        return raw
    val = round(val, 6)
    if val == 0.0:
        return "0"
    return f"{val:.6f}".rstrip("0").rstrip(".") or "0"


def normalize_numeric_tokens(node, parent_head: str = ""):
    """Normalize coordinate and geometry numbers in parsed S-expressions, leaving text and net names untouched."""
    if not isinstance(node, list) or not node:
        return canonical_num_token(node) if parent_head in _NUMERIC_SEXP_HEADS else node
    head = str(node[0])
    return [node[0], *(normalize_numeric_tokens(part, head) for part in node[1:])]


def _is_hidden_property(prop_node: list) -> bool:
    return any(part == "hide" or part == ["hide", "yes"] for part in prop_node[3:])


def canonicalize_guard_node(node, net_map: dict[str, str], parent_head: str = "", fp_lib: str = ""):
    """Normalize KiCad 8/9 vs KiCad 10 harmless serialization diffs after angle normalization."""
    if not isinstance(node, list) or not node:
        return canonical_num_token(node) if parent_head in _NUMERIC_SEXP_HEADS else node
    head = node[0]
    if head == "net" and len(node) >= 3:
        return ["net", str(node[2])]
    if head == "net" and len(node) == 2 and parent_head in ("zone", "segment", "arc", "via"):
        tok = str(node[1])
        if tok in net_map:
            return ["net", net_map[tok]]
    cur_lib = node[1] if head == "footprint" and len(node) > 1 and isinstance(node[1], str) else fp_lib
    out = [head]
    for part in node[1:]:
        if isinstance(part, list) and part:
            if part[0] in ("uuid", "tstamp"):
                continue
            if head == "footprint":
                if (part[0] in FOOTPRINT_HEADER or part == ["embedded_fonts", "no"]
                        or part == ["duplicate_pad_numbers_are_jumpers", "no"]):
                    continue
                if part[0] == "property" and len(part) >= 3 and _is_hidden_property(part):
                    if part[1] == "Description" and part[2] == "":
                        continue
                    if part[1] == "Footprint" and part[2] == cur_lib:
                        continue
            if head == "setup":
                if part == ["zone_defaults"] or part[0] == "pcbplotparams" or part in _KICAD10_SETUP_DEFAULTS:
                    continue
        out.append(canonicalize_guard_node(part, net_map, str(head), cur_lib))
    if head == "attr" and len(out) > 2:
        out = [head] + sorted(out[1:], key=repr)
    elif head == "fp_text" and len(out) > 3:
        out = out[:3] + sorted(out[3:], key=repr)
    elif head == "footprint" and len(out) > 2:
        out = out[:2] + sorted(out[2:], key=repr)
    return out


def guard_item_frozen(item, net_map: dict[str, str]):
    """Angle-normalize, canonicalize KiCad 8->10 diffs, and freeze one board S-expression item."""
    return frozen_sexp(canonicalize_guard_node(kicad_sexp.normalize_angles(item), net_map))


def purchasing_footprint_row(fp: list) -> list:
    """Return canonical purchasing row for one footprint, ignoring KiCad 10 boilerplate."""
    lib_id = fp[1] if len(fp) > 1 else None
    fields = sorted(
        (p[1], p[2]) for p in kicad_sexp.children(fp, "property")
        if len(p) >= 3 and not (p[1] == "Description" and p[2] == "") and not (p[1] == "Footprint" and p[2] == lib_id)
    )
    attr = kicad_sexp.child(fp, "attr")
    if attr and len(attr) > 2:
        attr = [attr[0], *sorted(attr[1:], key=repr)]
    pins = sorted((pad[1], pad[2]) for pad in kicad_sexp.children(fp, "pad") if len(pad) > 2)
    return [lib_id, fields, attr, kicad_sexp.child(fp, "layer"), pins]
