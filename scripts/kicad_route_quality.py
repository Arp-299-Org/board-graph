#!/usr/bin/env python3
"""Geometry-quality checks and canonical 45-degree routing helpers for KiCad.

KiCad DRC checks electrical and fabrication constraints.  This module adds the
visual/route-shape contract the board graph routes to: Manhattan/45-degree geometry,
no hard 90-degree corners, no backtracking, no redundant micro-segments, and
no pad its own net's tracks only graze: each track touching a pad must lay its
centre line into it, not run along its edge or clip its corner.
A track arc is outside those rules and is reported as an error, never judged:
route it as segments, or judge it by eye and say so.
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Iterable, Sequence

import kicad_rules_geom as rg


Point = tuple[float, float]
Direction = tuple[int, int]
ANGLE_TOLERANCE_MM = 0.002
DEFAULT_MIN_SEGMENT_MM = 0.15


@dataclass(frozen=True)
class Finding:
    severity: str
    rule: str
    net: str
    layer: int
    point_mm: Point
    message: str


@dataclass(frozen=True)
class PadCopper:
    """A standard pad's solid copper outline on one layer, in board coordinates."""

    center: Point
    size: Point
    angle_deg: float
    shape: str
    round_ratio: float = 0.0
    label: str = ""  # the part's reference and the pad's number, as "ENC1.1"

    def intersects_segment(self, start: Point, end: Point) -> bool:
        """Whether a centreline crosses this standard convex pad, even with both ends outside."""
        if self.shape not in {"rect", "roundrect", "oval", "circle"}:
            return False
        radius = (min(self.size) * self.round_ratio if self.shape == "roundrect" else
                  min(self.size) / 2 if self.shape in {"oval", "circle"} else 0.0)
        hx, hy = self.size[0] / 2 - radius, self.size[1] / 2 - radius
        a = rg.world_to_local(self.center[0], self.center[1], self.angle_deg, start[0], start[1])
        b = rg.world_to_local(self.center[0], self.center[1], self.angle_deg, end[0], end[1])
        delta = (b[0] - a[0], b[1] - a[1])
        low, high = 0.0, 1.0
        for origin, step, half in zip(a, delta, (hx, hy)):
            if abs(step) < 1e-12:
                if abs(origin) > half:
                    high = -1.0
            else:
                first, last = sorted(((-half - origin) / step, (half - origin) / step))
                low, high = max(low, first), min(high, last)
        if low <= high:
            return True
        distances = [math.hypot(max(abs(p[0]) - hx, 0), max(abs(p[1]) - hy, 0)) for p in (a, b)]
        length2 = delta[0] ** 2 + delta[1] ** 2
        for x, y in ((-hx, -hy), (-hx, hy), (hx, -hy), (hx, hy)):
            t = max(0.0, min(1.0, ((x - a[0]) * delta[0] + (y - a[1]) * delta[1]) / length2)) if length2 else 0.0
            distances.append(math.hypot(x - a[0] - t * delta[0], y - a[1] - t * delta[1]))
        return min(distances) <= radius + 1e-10

    def signed_distance(self, point: Point) -> float | None:
        """How far a point lies outside the pad's copper outline: negative inside, by its depth. None for a shape
        this check does not know, which can prove nothing."""
        return rg.pad_signed_distance(
            point[0], point[1], self.center[0], self.center[1], self.angle_deg,
            self.shape, self.size[0], self.size[1], self.round_ratio,
        )

    def contains_endcap(self, point: Point, radius: float) -> bool:
        """Test the entire round track endcap, not just its centre or pad bbox.

        Unsupported shapes cannot establish coverage. Drilled holes are removed
        from track and pad alike, so the shared drill does not expose track copper.
        """
        distance = self.signed_distance(point)
        return radius >= 0 and distance is not None and distance <= -radius + 1e-10

    def deepest_on_segment(self, start: Point, end: Point) -> float | None:
        """The depth inside the pad of the centre line's deepest point (negative when it stays outside). The outline
        is convex, so depth along a straight line rises to one peak and falls: a ternary search finds it."""
        if self.signed_distance(start) is None:
            return None
        def depth(t: float) -> float:
            return -self.signed_distance((start[0] + (end[0] - start[0]) * t, start[1] + (end[1] - start[1]) * t))
        low, high = 0.0, 1.0
        for _ in range(80):
            first, second = low + (high - low) / 3, high - (high - low) / 3
            if depth(first) < depth(second):
                low = first
            else:
                high = second
        return max(depth(0.0), depth(1.0), depth((low + high) / 2))


def _covered_by_pad(pads: dict[tuple[str, int], list[PadCopper]], net: str, layer: int,
                    point: Point, radius: float) -> bool:
    return bool(net) and any(pad.contains_endcap(point, radius) for pad in pads.get((net, layer), ()))


def _covered_by_via(vias: dict[str, list[tuple[Point, float]]], net: str, point: Point, radius: float,
                    directions: tuple[Direction, Direction]) -> bool:
    """A corner is the via ring's copper, not a corner in the outline, when the inside corner of the two tracks
    lies under the annulus: that point sits on the bisector, half a width divided by the sine of half the angle
    between the tracks from the node. At 90 degrees that is 1.41 half-widths, inside a 0.6 mm ring for a 0.3 mm
    track; at 45 degrees it is 2.6 half-widths, outside it, so an acute meeting at a via still surfaces."""
    if not net:
        return False
    (ax, ay), (bx, by) = directions
    cosine = (ax * bx + ay * by) / (math.hypot(ax, ay) * math.hypot(bx, by))
    angle = math.acos(max(-1.0, min(1.0, cosine)))
    if angle < 1e-9:
        return False  # a reversing pair: no inside corner the ring could cover
    reach = radius / math.sin(angle / 2)
    return any(math.dist(point, centre) + reach <= ring + 1e-9 for centre, ring in vias.get(net, ()))


def _point_to_segment_distance(point: Point, start: Point, end: Point) -> float:
    return rg.dist_point_to_seg(point[0], point[1], start[0], start[1], end[0], end[1])


def _buried_in_same_net_copper(
    net: str,
    index: int,
    a: Point,
    b: Point,
    width: float,
    pads: Sequence[PadCopper],
    vias: Sequence[tuple[Point, float]],
    layer_segments: Sequence[tuple[Point, Point, float]],
) -> bool:
    """Whether a segment's full round-capped copper lies inside another single same-net copper shape."""
    if not net:
        return False
    radius = width / 2
    if any(pad.contains_endcap(a, radius) and pad.contains_endcap(b, radius) for pad in pads):
        return True
    if any(math.dist(a, centre) + radius <= ring + 1e-6 and math.dist(b, centre) + radius <= ring + 1e-6
           for centre, ring in vias):
        return True
    for other_index, (p, q, other_width) in enumerate(layer_segments):
        if other_index == index or other_width < width - 1e-6:
            continue
        # Two duplicate micro-segments on top of each other must not hide each other.
        if other_width <= width + 1e-6 and math.dist(p, q) < DEFAULT_MIN_SEGMENT_MM:
            continue
        other_radius = other_width / 2
        if (_point_to_segment_distance(a, p, q) + radius <= other_radius + 1e-6
                and _point_to_segment_distance(b, p, q) + radius <= other_radius + 1e-6):
            return True
    return False


def _collinear_junction_run_length(
    net: str,
    layer: int,
    start_index: int,
    a: Point,
    b: Point,
    direction: Direction,
    width: float,
    length: float,
    incident: dict[tuple[str, int, Point], list[tuple[int, Direction, Point, float, float]]],
) -> float:
    """Combined length of a straight same-width run split only by T-junctions (degree >= 3)."""
    visited = {start_index}
    total = length
    for endpoint, step_dir in ((a, (-direction[0], -direction[1])), (b, direction)):
        current = endpoint
        while True:
            attached = incident[(net, layer, current)]
            if len(attached) < 3:
                break
            nxt = next(
                (entry for entry in attached
                 if entry[0] not in visited and entry[1] == step_dir
                 and math.isclose(entry[3], width, abs_tol=1e-6)),
                None,
            )
            if nxt is None:
                break
            visited.add(nxt[0])
            total += nxt[4]
            current = nxt[2]
    return total


def _micro_segment_findings(
    valid_segments: Sequence[tuple[str, int, int, Point, Point, Direction, float]],
    pad_points: dict[str, set[Point]],
    via_points: dict[str, set[Point]],
    pad_copper: dict[tuple[str, int], list[PadCopper]],
    via_rings: dict[str, list[tuple[Point, float]]],
    segments: dict[tuple[str, int], list[tuple[Point, Point, float]]],
) -> list[Finding]:
    """Flag unexplained short segments while ignoring terminal fixes, buried copper, and T-junction splits."""
    incident: dict[tuple[str, int, Point], list[tuple[int, Direction, Point, float, float]]] = defaultdict(list)
    for seg_id, (net, layer, _layer_idx, a, b, direction, width) in enumerate(valid_segments):
        length = math.dist(a, b)
        incident[(net, layer, a)].append((seg_id, direction, b, width, length))
        incident[(net, layer, b)].append((seg_id, (-direction[0], -direction[1]), a, width, length))

    findings: list[Finding] = []
    for seg_id, (net, layer, layer_idx, a, b, direction, width) in enumerate(valid_segments):
        length = math.dist(a, b)
        if length >= DEFAULT_MIN_SEGMENT_MM:
            continue
        terminals = pad_points[net] | via_points[net]
        if a in terminals or b in terminals:
            continue
        if _buried_in_same_net_copper(
            net, layer_idx, a, b, width,
            pad_copper.get((net, layer), ()),
            via_rings.get(net, ()),
            segments.get((net, layer), ()),
        ):
            continue
        if _collinear_junction_run_length(
            net, layer, seg_id, a, b, direction, width, length, incident
        ) >= DEFAULT_MIN_SEGMENT_MM:
            continue
        findings.append(Finding("warning", "micro_segment", net, layer, a, f"{length:.4f} mm: {a} -> {b}"))
    return findings


def _node_findings(nodes: dict[tuple[str, int, Point], list[tuple[Direction, float]]],
                   pads: dict[tuple[str, int], list[PadCopper]],
                   vias: dict[str, list[tuple[Point, float]]] | None = None) -> list[Finding]:
    findings: list[Finding] = []
    for (net, layer, point), segments in nodes.items():
        if len(segments) != 2:
            continue
        directions = sorted(segment[0] for segment in segments)
        radius = max(segment[1] for segment in segments) / 2
        if _covered_by_pad(pads, net, layer, point, radius) or _covered_by_via(vias or {}, net, point, radius,
                                                                               (directions[0], directions[1])):
            continue
        change = heading_change(directions[0], directions[1])
        if change == 2:
            findings.append(Finding("error", "hard_90_corner", net, layer, point, f"directions {directions}"))
        elif change <= 1:
            findings.append(Finding("error", "acute_or_reversing_corner", net, layer, point, f"directions {directions}"))
        elif change == 4 and math.isclose(segments[0][1], segments[1][1], abs_tol=1e-6):
            findings.append(Finding("warning", "mergeable_collinear_segments", net, layer, point, "merge into one segment"))
    return findings


def _pad_entry_findings(segments: dict[tuple[str, int], list[tuple[Point, Point, float]]],
                        pads: dict[tuple[str, int], list[PadCopper]]) -> list[Finding]:
    """A pad whose own net's tracks touch it on a layer, none of them entering it: the track that only grazes an edge
    or clips a corner, often laid for a part that has since changed. KiCad's design check sees copper joined and passes
    it. A track enters when its centre line gets half its width inside the pad, or the pad's own middle when the pad is
    narrower than that; a track whose centre line crosses the pad and leaves it again (both of its ends outside, or
    going on in another track) enters when it gets half that depth. Each copper layer of a pad is judged alone: a
    through-hole pad grazed on the back is flagged even when its front is entered properly. Pours, vias and arcs are
    not read: a pour or a via in the pad joins it, but the grazing track is still laid wrong; a track arc is already an
    error above. A pad shape this check does not know is not judged."""
    findings: list[Finding] = []
    for (net, layer), coppers in pads.items():
        tracks = segments.get((net, layer), ())
        if not net or not tracks:
            continue
        at: dict[Point, list[tuple[Point, Point, float]]] = defaultdict(list)
        for track in tracks:
            at[track[0]].append(track)
            at[track[1]].append(track)
        for pad in coppers:
            def outside(point: Point) -> bool:
                return pad.signed_distance(point) >= 0
            def leaves(track: tuple[Point, Point, float]) -> bool:
                """Both ends outside the pad, or inside it where another track goes on and out of it."""
                return all(outside(end) or any(other is not track and outside(other[0] if other[1] == end else other[1])
                                               for other in at[end]) for end in track[:2])
            if pad.signed_distance(pad.center) is None:
                continue
            reach = math.hypot(*pad.size) / 2
            touching, best = 0, None
            entered = False
            for track in tracks:
                a, b, width = track
                margin = reach + width / 2
                if (max(a[0], b[0]) < pad.center[0] - margin or min(a[0], b[0]) > pad.center[0] + margin or
                        max(a[1], b[1]) < pad.center[1] - margin or min(a[1], b[1]) > pad.center[1] + margin):
                    continue
                depth = pad.deepest_on_segment(a, b)
                if depth is None or depth <= -width / 2 + 1e-6:
                    continue  # its copper does not reach the pad
                touching += 1
                need = min(width / 2, min(pad.size) / 2)
                if depth >= need - 1e-6 or (depth >= need / 2 - 1e-6 and leaves(track)):
                    entered = True
                    break
                best = depth if best is None else max(best, depth)
            if touching and not entered:
                findings.append(Finding(
                    "error", "track_grazes_pad", net, layer, pad.center,
                    f"{pad.label or 'pad'}: {touching} track(s) of its net touch it and none enters it; the deepest "
                    f"centre line is {round(best, 3) + 0.0:+.3f} mm inside: lay the track into the pad"))
    return findings


def _sign(value: float, tolerance: float = ANGLE_TOLERANCE_MM) -> int:
    if abs(value) <= tolerance:
        return 0
    return 1 if value > 0 else -1


def compass_direction(a: Point, b: Point) -> tuple[int, int] | None:
    """Return an 8-way direction, or None for an arbitrary-angle segment."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    sx, sy = _sign(dx), _sign(dy)
    if sx == 0 and sy == 0:
        return (0, 0)
    if sx == 0 or sy == 0:
        return (sx, sy)
    if abs(abs(dx) - abs(dy)) <= ANGLE_TOLERANCE_MM:
        return (sx, sy)
    return None


_HEADING = {
    (1, 0): 0,
    (1, 1): 1,
    (0, 1): 2,
    (-1, 1): 3,
    (-1, 0): 4,
    (-1, -1): 5,
    (0, -1): 6,
    (1, -1): 7,
}


def heading_change(first: tuple[int, int], second: tuple[int, int]) -> int:
    delta = abs(_HEADING[first] - _HEADING[second])
    return min(delta, 8 - delta)


def validate_polyline(
    points: Sequence[Point],
    *,
    min_segment_mm: float = DEFAULT_MIN_SEGMENT_MM,
    allow_terminal_corrections: bool = True,
    allow_reverse_progress: bool = False,
) -> list[str]:
    """Validate one ordered route before it is written to KiCad."""
    errors: list[str] = []
    if len(points) < 2:
        return ["route must contain at least two points"]

    directions: list[tuple[int, int] | None] = []
    overall = (points[-1][0] - points[0][0], points[-1][1] - points[0][1])
    for index, (a, b) in enumerate(zip(points, points[1:])):
        length = math.dist(a, b)
        direction = compass_direction(a, b)
        directions.append(direction)
        if direction == (0, 0):
            errors.append(f"segment {index} has duplicate endpoints {a}")
        elif direction is None:
            errors.append(f"segment {index} is not horizontal, vertical, or 45 degrees: {a} -> {b}")
        terminal = index in {0, len(points) - 2}
        if length < min_segment_mm and not (allow_terminal_corrections and terminal):
            errors.append(f"segment {index} is an unexplained {length:.4f} mm micro-segment")
        if not allow_reverse_progress and (overall[0] or overall[1]):
            step = (b[0] - a[0], b[1] - a[1])
            if step[0] * overall[0] + step[1] * overall[1] < -ANGLE_TOLERANCE_MM:
                errors.append(f"segment {index} backtracks away from the destination: {a} -> {b}")

    for index, (first, second) in enumerate(zip(directions, directions[1:]), start=1):
        if first in {None, (0, 0)} or second in {None, (0, 0)}:
            continue
        change = heading_change(first, second)
        if change == 0:
            errors.append(f"point {index} is a redundant collinear waypoint: {points[index]}")
        elif change == 2:
            errors.append(f"point {index} creates a hard 90-degree corner: {points[index]}")
        elif change >= 3:
            errors.append(f"point {index} creates an acute/reversing turn: {points[index]}")
    return errors


def canonical_45_path(start: Point, end: Point, balance: float = 0.5) -> list[Point]:
    """Return a compact H-45-H or V-45-V path between two points.

    ``balance`` chooses how the non-diagonal distance is split before and after
    the diagonal.  Zero or one yields a two-segment path; 0.5 is symmetric.
    """
    if not 0.0 <= balance <= 1.0:
        raise ValueError("balance must be between 0 and 1")
    dx, dy = end[0] - start[0], end[1] - start[1]
    if abs(dx) <= ANGLE_TOLERANCE_MM or abs(dy) <= ANGLE_TOLERANCE_MM or abs(abs(dx) - abs(dy)) <= ANGLE_TOLERANCE_MM:
        return [start, end]
    sx, sy = (1 if dx > 0 else -1), (1 if dy > 0 else -1)
    points = [start]
    if abs(dx) > abs(dy):
        extra = abs(dx) - abs(dy)
        first = extra * balance
        if first > ANGLE_TOLERANCE_MM:
            points.append((start[0] + sx * first, start[1]))
        diagonal_end = (start[0] + sx * (first + abs(dy)), end[1])
        points.append(diagonal_end)
    else:
        extra = abs(dy) - abs(dx)
        first = extra * balance
        if first > ANGLE_TOLERANCE_MM:
            points.append((start[0], start[1] + sy * first))
        diagonal_end = (end[0], start[1] + sy * (first + abs(dx)))
        points.append(diagonal_end)
    if math.dist(points[-1], end) > ANGLE_TOLERANCE_MM:
        points.append(end)
    return points


def _mm(vector) -> Point:
    return (vector.x / 1_000_000.0, vector.y / 1_000_000.0)


def _is_live_arc(item) -> bool:
    """kipy lists arcs among the tracks: an ArcTrack carries a midpoint, a straight Track does not."""
    return hasattr(item, "mid")


def _arc_finding(net: str, layer: int, a: Point, b: Point) -> Finding:
    return Finding("error", "track_arc", net, layer, a,
                   f"track arc {a} -> {b}: outside the 45-degree rules and not judged by this check; "
                   "route it as segments or judge it by eye")


def _live_pad_copper(board, pads, layers: set[int],
                     labels: dict[int, str] | None = None) -> dict[tuple[str, int], list[PadCopper]]:
    from kipy.proto.board.board_types_pb2 import BoardLayer, PadStackShape, PadStackType

    out: dict[tuple[str, int], list[PadCopper]] = defaultdict(list)
    if not pads or not layers:
        return out
    # This also accounts for layer-removal settings. A normal padstack supplies
    # one geometry entry for all its copper layers, not one entry per layer.
    presence = board.check_padstack_presence_on_layers(pads, list(layers))
    names = {PadStackShape.PSS_RECTANGLE: "rect", PadStackShape.PSS_CIRCLE: "circle",
             PadStackShape.PSS_OVAL: "oval", PadStackShape.PSS_ROUNDRECT: "roundrect"}
    for pad in pads:
        if not pad.net.name:
            continue
        stack = pad.padstack
        geometry = {int(item.layer): item for item in stack.copper_layers}
        for layer, present in presence.get(pad, {}).items():
            if not present or layer not in layers:
                continue
            if stack.type == PadStackType.PST_NORMAL:
                shape = geometry.get(BoardLayer.BL_F_Cu)
            elif stack.type == PadStackType.PST_FRONT_INNER_BACK:
                shape = geometry.get(layer if layer in (BoardLayer.BL_F_Cu, BoardLayer.BL_B_Cu)
                                     else BoardLayer.BL_In1_Cu)
            else:
                shape = geometry.get(layer)
            if shape is None or shape.shape not in names:
                continue  # Unknown geometry cannot prove the endcap is buried.
            angle = stack.angle.degrees
            ox, oy = _mm(shape.offset)
            px, py = _mm(pad.position)
            center = rg.local_to_world(px, py, angle, ox, oy)
            out[(pad.net.name, int(layer))].append(
                PadCopper(center, _mm(shape.size), angle, names[shape.shape], shape.corner_rounding_ratio,
                          (labels or {}).get(id(pad), ""))
            )
    return out


def _offline_pad_copper(pads, layers: set[int], pcbnew,
                        labels: dict[int, str] | None = None) -> dict[tuple[str, int], list[PadCopper]]:
    out: dict[tuple[str, int], list[PadCopper]] = defaultdict(list)
    names = {pcbnew.PAD_SHAPE_RECTANGLE: "rect", pcbnew.PAD_SHAPE_CIRCLE: "circle",
             pcbnew.PAD_SHAPE_OVAL: "oval", pcbnew.PAD_SHAPE_ROUNDRECT: "roundrect"}
    for pad in pads:
        if not pad.GetNetname():
            continue
        for layer in layers:
            if not pad.IsOnLayer(layer) or (hasattr(pad, "FlashLayer") and not pad.FlashLayer(layer)):
                continue
            shape = pad.GetShape(layer)
            if shape not in names:
                continue
            center, size = pad.ShapePos(layer), pad.GetSize(layer)
            ratio = pad.GetRoundRectRadiusRatio(layer) if names[shape] == "roundrect" else 0.0
            out[(pad.GetNetname(), int(layer))].append(PadCopper(
                (pcbnew.ToMM(center.x), pcbnew.ToMM(center.y)),
                (pcbnew.ToMM(size.x), pcbnew.ToMM(size.y)),
                pad.GetOrientationDegrees(), names[shape], ratio, (labels or {}).get(id(pad), "")))
    return out


def audit_live_board(
    project: pathlib.Path,
    board=None,
    *,
    footprints=None,
    tracks=None,
    vias=None,
) -> tuple[list[Finding], dict[str, int]]:
    if board is None:
        from kipy import KiCad

        board = KiCad(timeout_ms=10_000).get_board()
    if pathlib.Path(str(board.document.project.path)) != project:
        raise RuntimeError(f"Refusing wrong live project: {board.document.project.path}")

    findings: list[Finding] = []
    nodes: dict[tuple[str, int, Point], list[tuple[Direction, float]]] = defaultdict(list)
    pad_points: dict[str, set[Point]] = defaultdict(set)
    pads, labels = [], {}
    fp_list = board.get_footprints() if footprints is None else footprints
    for footprint in fp_list:
        for pad in footprint.definition.pads:
            pads.append(pad)
            labels[id(pad)] = f"{footprint.reference_field.text.value}.{pad.number}"
            pad_points[pad.net.name].add(tuple(round(value, 6) for value in _mm(pad.position)))

    via_list = list(board.get_vias() if vias is None else vias)
    via_points: dict[str, set[Point]] = defaultdict(set)
    via_rings: dict[str, list[tuple[Point, float]]] = defaultdict(list)
    for via in via_list:
        centre = tuple(round(value, 6) for value in _mm(via.position))
        via_points[via.net.name].add(centre)
        via_rings[via.net.name].append((centre, via.diameter / 2_000_000.0))

    items = list(board.get_tracks() if tracks is None else tracks)
    arcs = [item for item in items if _is_live_arc(item)]
    track_items = [item for item in items if not _is_live_arc(item)]
    for arc in arcs:
        findings.append(_arc_finding(arc.net.name, int(arc.layer),
                                     tuple(round(value, 6) for value in _mm(arc.start)),
                                     tuple(round(value, 6) for value in _mm(arc.end))))
    pad_copper = _live_pad_copper(board, pads, {int(track.layer) for track in track_items}, labels)
    segments: dict[tuple[str, int], list[tuple[Point, Point, float]]] = defaultdict(list)
    valid_segments: list[tuple[str, int, int, Point, Point, Direction, float]] = []
    for track in track_items:
        net, layer = track.net.name, int(track.layer)
        a = tuple(round(value, 6) for value in _mm(track.start))
        b = tuple(round(value, 6) for value in _mm(track.end))
        width = track.width / 1_000_000.0
        layer_idx = -1
        if a != b:
            layer_idx = len(segments[(net, layer)])
            segments[(net, layer)].append((a, b, width))
        direction = compass_direction(a, b)
        if direction is None:
            findings.append(Finding("error", "non_45_segment", net, layer, a, f"{a} -> {b}"))
            continue
        if direction == (0, 0):
            findings.append(Finding("error", "zero_length_segment", net, layer, a, f"{a} -> {b}"))
            continue
        valid_segments.append((net, layer, layer_idx, a, b, direction, width))
        nodes[(net, layer, a)].append((direction, width))
        nodes[(net, layer, b)].append(((-direction[0], -direction[1]), width))

    findings.extend(_micro_segment_findings(valid_segments, pad_points, via_points, pad_copper, via_rings, segments))
    findings.extend(_node_findings(nodes, pad_copper, via_rings))
    findings.extend(_pad_entry_findings(segments, pad_copper))

    summary = {
        "tracks": len(track_items),
        "arcs": len(arcs),
        "vias": len(via_list),
        "errors": sum(item.severity == "error" for item in findings),
        "warnings": sum(item.severity == "warning" for item in findings),
    }
    return findings, summary


def _via_width(via, pcbnew) -> int:
    """The via's ring diameter in KiCad units: KiCad 10 asks for a layer, older bindings do not."""
    try:
        return via.GetWidth(getattr(pcbnew, "F_Cu", 0))  # front copper is layer 0
    except TypeError:
        return via.GetWidth()


def audit_offline_board(board_path: pathlib.Path) -> tuple[list[Finding], dict[str, int]]:
    """Audit a saved board without requiring KiCad's live IPC service."""
    import pcbnew

    board = pcbnew.LoadBoard(str(board_path))
    findings: list[Finding] = []
    nodes: dict[tuple[str, int, Point], list[tuple[Direction, float]]] = defaultdict(list)
    pad_points: dict[str, set[Point]] = defaultdict(set)
    pads, labels = [], {}
    for footprint in board.GetFootprints():
        for pad in footprint.Pads():
            pads.append(pad)
            labels[id(pad)] = f"{footprint.GetReference()}.{pad.GetNumber()}"
            position = pad.GetPosition()
            pad_points[pad.GetNetname()].add(
                (round(pcbnew.ToMM(position.x), 6), round(pcbnew.ToMM(position.y), 6))
            )

    track_items = list(board.GetTracks())
    vias = [item for item in track_items if isinstance(item, pcbnew.PCB_VIA)]
    via_points: dict[str, set[Point]] = defaultdict(set)
    via_rings: dict[str, list[tuple[Point, float]]] = defaultdict(list)
    for via in vias:
        position = via.GetPosition()
        centre = (round(pcbnew.ToMM(position.x), 6), round(pcbnew.ToMM(position.y), 6))
        via_points[via.GetNetname()].add(centre)
        via_rings[via.GetNetname()].append((centre, pcbnew.ToMM(_via_width(via, pcbnew)) / 2))

    tracks = [item for item in track_items if type(item) is pcbnew.PCB_TRACK]
    arcs = [item for item in track_items if isinstance(item, getattr(pcbnew, "PCB_ARC", ()))]
    for arc in arcs:
        start, end = arc.GetStart(), arc.GetEnd()
        findings.append(_arc_finding(arc.GetNetname(), int(arc.GetLayer()),
                                     (round(pcbnew.ToMM(start.x), 6), round(pcbnew.ToMM(start.y), 6)),
                                     (round(pcbnew.ToMM(end.x), 6), round(pcbnew.ToMM(end.y), 6))))
    pad_copper = _offline_pad_copper(pads, {int(track.GetLayer()) for track in tracks}, pcbnew, labels)
    segments: dict[tuple[str, int], list[tuple[Point, Point, float]]] = defaultdict(list)
    valid_segments: list[tuple[str, int, int, Point, Point, Direction, float]] = []
    for track in tracks:
        net, layer = track.GetNetname(), int(track.GetLayer())
        start, end = track.GetStart(), track.GetEnd()
        a = (round(pcbnew.ToMM(start.x), 6), round(pcbnew.ToMM(start.y), 6))
        b = (round(pcbnew.ToMM(end.x), 6), round(pcbnew.ToMM(end.y), 6))
        width = pcbnew.ToMM(track.GetWidth())
        layer_idx = -1
        if a != b:
            layer_idx = len(segments[(net, layer)])
            segments[(net, layer)].append((a, b, width))
        direction = compass_direction(a, b)
        if direction is None:
            findings.append(Finding("error", "non_45_segment", net, layer, a, f"{a} -> {b}"))
            continue
        if direction == (0, 0):
            findings.append(Finding("error", "zero_length_segment", net, layer, a, f"{a} -> {b}"))
            continue
        valid_segments.append((net, layer, layer_idx, a, b, direction, width))
        nodes[(net, layer, a)].append((direction, width))
        nodes[(net, layer, b)].append(((-direction[0], -direction[1]), width))

    findings.extend(_micro_segment_findings(valid_segments, pad_points, via_points, pad_copper, via_rings, segments))
    findings.extend(_node_findings(nodes, pad_copper, via_rings))
    findings.extend(_pad_entry_findings(segments, pad_copper))

    summary = {
        "tracks": len(tracks),
        "arcs": len(arcs),
        "vias": len(vias),
        "errors": sum(item.severity == "error" for item in findings),
        "warnings": sum(item.severity == "warning" for item in findings),
    }
    return findings, summary


def main() -> None:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--project", type=pathlib.Path)
    source.add_argument("--board", type=pathlib.Path)
    parser.add_argument("--output", type=pathlib.Path)
    parser.add_argument("--no-fail", action="store_true")
    args = parser.parse_args()
    if args.board:
        findings, summary = audit_offline_board(args.board.resolve())
    else:
        findings, summary = audit_live_board(args.project.resolve())
    report = {"schema": "board-graph-route-quality/v1", "summary": summary, "findings": [asdict(item) for item in findings]}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, sort_keys=True))
    for item in findings[:50]:
        print(f"{item.severity}: {item.rule}: {item.net} layer={item.layer} at={item.point_mm}: {item.message}")
    if summary["errors"] and not args.no_fail:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
