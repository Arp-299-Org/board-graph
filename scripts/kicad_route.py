#!/usr/bin/env python3
"""Drive KiCad's real interactive router on the Mac by pad and net name, not by pixels.

  py -3 scripts/kicad_route.py R15.1 R16.2                     route one connection in the open PCB Editor
  py -3 scripts/kicad_route.py R15.1 R16.2 --via 120,80 --through 125,85 --layer F.Cu --save
  py -3 scripts/kicad_route.py R36.2 @120.7965,100.0               end on a via or track end of R36.2's net
  py -3 scripts/kicad_route.py ENC1.2 via@204.358,96.05            end on a new via there (into a plane, say)
  py -3 scripts/kicad_route.py SW1.1@117.365,101.11 R20.1          one of several pads numbered 1, by its position

--via and --through are board positions in mm, taken in the order written. With several PCB Editors open, name
one with --pid (from `mac.py windows`). It prints one JSON result and exits 0 only when the connection closed.

KiCad's router finds the path and lays the copper: the tool hovers, presses X, clicks and presses Esc. IPC reads
(pads, nets, tracks), frames the view and sets the active layer, and makes one exact edit: a click lands up to about
0.02 mm off, so a leg meant to run on a pad's axis can end microns off it, with a tiny diagonal back. The tool then
takes out each segment shorter than 0.05 mm of the new copper, meets its neighbours on their own 45-degree headings,
and leaves pad centres, older vias and older copper where they are; a via this run placed, with one track on it,
slides with that track's end. Nothing moves more than 0.05 mm, and it is one undo step, read back. A board position becomes a screen point through
a mapping fitted from the status bar's own X and Y, and the status bar is read again before each click, so a wrong
mapping stops the run instead of clicking elsewhere. A run that does not close is undone with the editor's undo,
back to exactly the items it started from.
Surface-mount pad escapes and approaches follow the pad axes, with corners outside the pad outline.
"""
from __future__ import annotations

import argparse
import collections
import dataclasses
import hashlib
import json
import math
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import kicad_ipc  # noqa: E402
import mac  # noqa: E402
import site_settings  # noqa: E402
from kicad_route_quality import PadCopper, audit_live_board, compass_direction  # noqa: E402

VENV_PYTHON = f"{mac.CHECKOUT}/.venv-kicad-ipc/bin/python"
CACHE = mac.SITE.cache  # the Mac's cache folder, from the site settings
SITE_COPY = "board_graph_site.json"  # the site settings' copy beside the script run_from_elsewhere carries
ZOOM_STEP = 1.1  # one wheel step on the Mac: 10 steps took the zoom from 2.94 to 7.62
ZOOM_MOST = 10
WANT_SCALE = 25.0  # points per mm the view should reach when the whole connection fits at it
MAX_SCALE = 60.0  # points per mm; closer than this, a connection's surroundings are off screen
MIN_PAD_POINTS = 1.5  # half the smallest pad, in points, that can still be hovered reliably
SAFE = (0.20, 0.13, 0.84, 0.76)  # the part of the window always on the canvas: left, top, right, bottom fractions
STATUS_ROW = 24  # points from the window's bottom: the status bar
PANEL_ROW = (24, 68)  # points from the window's bottom: the message panel, where the router names its net
STRIP = (0.17, 0.08, 0.87, 0.16)  # the canvas's top, where KiCad's info bar says why the router refused
KEY_CODES = {"x": 7, "v": 9, "z": 6, "q": 12, "esc": 53, "f4": 118}
DIMENSIONS = ("Track and Via Dimensions", "Track & Via Dimensions")
OTHER_SIDE = {"F.Cu": "B.Cu", "B.Cu": "F.Cu"}
MODES = {None: "Walk around", "shove": "Shove"}  # --mode to the router's own name for it; no --mode is Walk around


def same_mode(shown: str | None, wanted: str) -> bool:
    clean = lambda name: re.sub(r"\s", "", name or "").lower()  # noqa: E731
    return clean(shown) == clean(wanted)


class RouteFailure(Exception):
    """The run stopped. `status` is a word a caller can branch on, and `reason` says what happened."""

    def __init__(self, status: str, reason: str, **extra):
        super().__init__(reason)
        self.status, self.reason, self.extra = status, reason, extra


# What the status bar and the message panel say, from the text OCR found in them.

@dataclasses.dataclass
class Status:
    x: float
    y: float
    zoom: float | None
    grid: float | None


@dataclasses.dataclass
class Panel:
    routing: bool
    net: str | None
    mode: str | None
    width: float | None


def parse_status(text: str) -> Status | None:
    """The status bar's X, Y, zoom and grid, or None if it did not read as a position in mm. OCR reads a 0 as O or
    o and a 1 as l or I, and X and Y in either case; a wrong digit is caught by the fit and the check at each hover."""
    number = r"(-?[\dOolI]+\.[\dOolI]+)"
    position = re.search(rf"(?<![A-Za-z])[Xx]\s*{number}\s+[Yy]\s*{number}", text)
    if not position or not re.search(r"\bmm\b", text):
        return None
    zoom = re.search(rf"(?<![A-Za-z])Z\s*{number}", text)
    grid = re.search(rf"\bgrid\s*{number}", text)
    value = lambda found: float(found.translate(str.maketrans("OolI", "0011")))  # noqa: E731
    return Status(value(position[1]), value(position[2]), value(zoom[1]) if zoom else None,
                  value(grid[1]) if grid else None)


def parse_panel(text: str) -> Panel:
    # OCR reads the colon after "Routing Track" as a colon, ".'" or nothing: skip any such marks before the net name.
    net = re.search(r"Routing Track[:.,;'`’\s]*([^\s:.,;'`’]\S*)", text)
    mode = re.search(r"Walk ?around|Shove|Highlight collisions", text, re.IGNORECASE)
    width = re.search(r"Track Width:?\s*(\d+\.\d+)", text)
    return Panel(bool(net), net[1] if net else None, mode[0] if mode else None, float(width[1]) if width else None)


def _clean_net(name: str) -> str:
    return re.sub(r"[\s_]", "", name).translate(str.maketrans("/Il|Oo", "111100"))


def _edits(a: str, b: str) -> int:
    """Letters to change, add or drop to turn a into b."""
    row = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        row, last = [i], row
        for j, y in enumerate(b, 1):
            row.append(min(last[j] + 1, row[j - 1] + 1, last[j - 1] + (x != y)))
    return row[-1]


NET_MARGIN = 2  # letters: the expected net must be at least this much nearer the reading than any other near net
# mm beyond the hover tolerance in which other copper counts as near the pointer: the router arms on copper under or
# just beside the pointer, and 0.5 mm is more than a 0.25 mm track and the 0.2 mm clearance, so copper of another net
# sitting one clearance from the aimed copper is always among the candidates.
NEAR_MARGIN = 0.5


def read_net(shown: str, expected: str, near, board=()) -> tuple[bool, list]:
    """Whether the panel's reading names the expected net, and the near nets nearest the reading with their distances.
    `near` is the nets of the copper near the pointer: those are the only nets the router can have armed on. An exact
    match after cleaning counts; otherwise the expected net must be the near net nearest the reading, within a quarter
    of its letters, and every other near net at least NET_MARGIN letters further. `board` is every net's name: a
    reading that is exactly another net's name stops even when that net is not near, since then the near list is wrong."""
    if same_net(shown, expected):
        return True, []
    if not near:
        return False, []  # with no list of the nets near the pointer, only an exact reading counts
    reading = _clean_net(shown)
    ranked = sorted((_edits(reading, _clean_net(name)), name) for name in set(near) | {expected})
    nearest = [f"{n} ({d})" for d, n in ranked[:3]]
    best, name = ranked[0]
    runner = ranked[1][0] if len(ranked) > 1 else best + NET_MARGIN
    ok = bool(reading) and name == expected and best <= max(1, len(reading) // 4) and runner - best >= NET_MARGIN
    exact = [other for other in board if other != expected and _clean_net(other) == reading]
    return ok and not exact, nearest + [f"{n} (0, not near)" for n in exact if n not in near]


def near_nets(point: tuple[float, float], items, pad_boxes, reach: float) -> list[str]:
    """The nets of the pads, tracks and vias whose copper comes within `reach` mm of the point, on any layer."""
    x, y = point
    found = set()
    for _, net, (x0, y0, x1, y1) in pad_boxes:
        if math.hypot(max(x0 - x, 0.0, x - x1), max(y0 - y, 0.0, y - y1)) <= reach:
            found.add(net)
    for item in items:
        if _distance_to_segment(point, item.start, item.end) - item.width / 2 <= reach:
            found.add(item.net)
    return sorted(net for net in found if net)


def same_net(shown: str, expected: str) -> bool:
    """Whether the panel's OCR text names this net. Only what OCR drops or confuses is forgiven (an underscore, a
    slash read as I, O read as 0), and a name the panel cut short with an ellipsis must match as far as it goes; any
    other different digit is a different net."""
    clean = _clean_net
    cut = re.split(r"\.\.\.|…", shown)
    if len(cut) > 1:
        return bool(clean(cut[0])) and clean(expected).startswith(clean(cut[0]))
    return clean(shown) == clean(expected)


def refusal_text(baseline: list[str], now: list[str]) -> str | None:
    """What appeared at the canvas's top since the baseline: the router's own words."""
    new = [re.sub(r"^\S\s+", "", line) for line in now if line not in baseline]  # the bar's icon reads as one letter
    return " ".join(new) if new else None


# Board positions to screen points.

@dataclasses.dataclass
class Mapping:
    """Screen = a * board + b on each axis. `error` is how far off, in mm, a board position may be."""
    ax: float
    bx: float
    ay: float
    by: float
    error: float

    def to_screen(self, x: float, y: float) -> tuple[float, float]:
        return self.ax * x + self.bx, self.ay * y + self.by

    @property
    def scale(self) -> float:
        return abs(self.ax)


def _line(points: list[tuple[float, float]]) -> tuple[float, float]:
    """Least-squares a, b for screen = a * board + b, from (board, screen) pairs."""
    n = len(points)
    mean_b = sum(p[0] for p in points) / n
    mean_s = sum(p[1] for p in points) / n
    spread = sum((p[0] - mean_b) ** 2 for p in points)
    if spread == 0:
        raise RouteFailure("mapping_failed", "the status bar did not change as the pointer moved: "
                           "the pointer may be off the canvas, or the status bar is not updating")
    a = sum((p[0] - mean_b) * (p[1] - mean_s) for p in points) / spread
    return a, mean_s - a * mean_b


def fit_mapping(samples: list[tuple[float, float, Status]], grid: float) -> Mapping:
    """Fit from (screen x, screen y, status) readings. The status bar snaps to the grid, so each reading may be
    half a grid off; anything worse, or an image stretched unequally, is not a view: it fails."""
    ax, bx = _line([(s.x, sx) for sx, _, s in samples])
    ay, by = _line([(s.y, sy) for _, sy, s in samples])
    if ax == 0 or ay == 0:
        raise RouteFailure("mapping_failed", "the status bar did not follow the pointer")
    worst = max(max(abs((sx - bx) / ax - s.x), abs((sy - by) / ay - s.y)) for sx, sy, s in samples)
    allowed = grid + 2 / abs(ax)
    if worst > allowed:
        raise RouteFailure("mapping_failed", f"the status bar readings disagree by {worst:.3f} mm (allowed "
                           f"{allowed:.3f}): {[(s.x, s.y) for _, _, s in samples]}")
    span = max(max(s.x for _, _, s in samples) - min(s.x for _, _, s in samples),
               max(s.y for _, _, s in samples) - min(s.y for _, _, s in samples))
    if abs(abs(ax) - abs(ay)) / abs(ax) > 2 * grid / span + 0.01:
        raise RouteFailure("mapping_failed", f"x and y scales differ: {ax:.3f} and {ay:.3f} points per mm")
    return Mapping(ax, bx, ay, by, grid / 2 + 1 / abs(ax))


def fits(mapping: Mapping, points: list[tuple[float, float]], safe: tuple[float, float, float, float],
         smallest_pad_half: float, closely: bool = True) -> bool:
    """Whether this view shows every point at a usable zoom and, `closely`, as close as WANT_SCALE when they would
    fit at that: then the picture shows the route, and a pad is many points wide."""
    left, top, right, bottom = safe
    if not all(left <= sx <= right and top <= sy <= bottom for sx, sy in (mapping.to_screen(*p) for p in points)):
        return False
    span_x = max(p[0] for p in points) - min(p[0] for p in points)
    span_y = max(p[1] for p in points) - min(p[1] for p in points)
    roomy = min((right - left) / max(span_x, 1e-3), (bottom - top) / max(span_y, 1e-3)) * 0.7
    if mapping.scale * smallest_pad_half < MIN_PAD_POINTS or mapping.scale > max(MAX_SCALE, roomy):
        return False
    return not closely or mapping.scale >= 0.7 * min(WANT_SCALE, roomy)


def zoom_steps(mapping: Mapping, points: list[tuple[float, float]], safe: tuple[float, float, float, float]) -> int:
    """Wheel steps, up to zoom in, that make these points fill most of the safe area, with the middle of them where
    it is now. At most ZOOM_MOST: the wheel's anchor is not quite under the pointer, so a big jump loses the spot."""
    left, top, right, bottom = safe
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    cx, cy = mapping.to_screen((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)
    room_x, room_y = max(1.0, min(cx - left, right - cx)), max(1.0, min(cy - top, bottom - cy))
    reach_x, reach_y = max((max(xs) - min(xs)) / 2, 1e-3), max((max(ys) - min(ys)) / 2, 1e-3)
    target = min(room_x / reach_x, room_y / reach_y, WANT_SCALE) * 0.9
    steps = math.log(target / mapping.scale) / math.log(ZOOM_STEP)
    return max(-ZOOM_MOST, min(ZOOM_MOST, int(math.floor(steps + 0.5))))


def on_rect(point: tuple[float, float], rect: tuple[float, float, float, float]) -> bool:
    return rect[0] <= point[0] <= rect[2] and rect[1] <= point[1] <= rect[3]


def within(reading: Status, x: float, y: float, tolerance: float) -> bool:
    return abs(reading.x - x) <= tolerance and abs(reading.y - y) <= tolerance


def refine_near(editor, mapping: Mapping, x: float, y: float, grid: float) -> Mapping:
    """Sharpen the mapping near one spot, by finding where the status bar's snapped reading flips from one grid
    point to the next: the midpoint of the two values lies exactly under the pointer there. Both axes at once."""
    pitch = grid * mapping.scale
    if pitch < 3:
        return mapping
    sx, sy = mapping.to_screen(x, y)
    half = pitch * 0.75
    low, high = [sx - half, sy - half], [sx + half, sy + half]
    first, last = editor.read_at(*low), editor.read_at(*high)
    read_low, read_high = [first.x, first.y], [last.x, last.y]
    if read_low[0] == read_high[0] or read_low[1] == read_high[1]:
        raise RouteFailure("mapping_failed", f"no grid step found near ({x:.3f}, {y:.3f}), between {read_low} and {read_high} "
                           f"at {pitch:.1f} points a grid: the mapping is off by more than a grid")
    while max(high[0] - low[0], high[1] - low[1]) > 0.6:
        mid = [(low[0] + high[0]) / 2, (low[1] + high[1]) / 2]
        now = editor.read_at(*mid)
        for axis, value in enumerate((now.x, now.y)):
            if value == read_low[axis]:
                low[axis], read_low[axis] = mid[axis], value
            else:
                high[axis], read_high[axis] = mid[axis], value
    boundary = [(read_low[i] + read_high[i]) / 2 for i in range(2)]
    pointer = [(low[i] + high[i]) / 2 for i in range(2)]
    return Mapping(mapping.ax, pointer[0] - mapping.ax * boundary[0], mapping.ay, pointer[1] - mapping.ay * boundary[1],
                   1 / mapping.scale)


# Pads, tracks and whether they connect.

@dataclasses.dataclass
class PadInfo:
    name: str
    net: str
    x: float
    y: float
    layers: tuple[str, ...]  # copper layers
    half: float  # half the pad's smaller side, mm
    through: bool
    size: tuple[float, float] | None = None
    angle: float = 0.0  # KiCad degrees: positive counterclockwise, board Y points down
    round: bool = False
    copper: PadCopper | None = None
    others: tuple = ()  # (x0, y0, x1, y1) of the same part's pads on other nets: an escape leg must keep clear of them
    track_end: bool = False  # an older track's end, from @X,Y: KiCad's router does not pull the pointer onto it


ESCAPE_GAP = 0.35  # mm from a planned leg's centre line to another net's pad: half a 0.25 mm track, 0.2 mm clearance, a margin


def _segment_box_distance(a: tuple[float, float], b: tuple[float, float], box: tuple[float, float, float, float]) -> float:
    x0, y0, x1, y1 = box
    inside = lambda p: x0 <= p[0] <= x1 and y0 <= p[1] <= y1  # noqa: E731
    if inside(a) or inside(b):
        return 0.0
    edges = [((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)), ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))]

    def crosses(p, q, r, s):
        d = (q[0] - p[0]) * (s[1] - r[1]) - (q[1] - p[1]) * (s[0] - r[0])
        if abs(d) < 1e-12:
            return False
        t = ((r[0] - p[0]) * (s[1] - r[1]) - (r[1] - p[1]) * (s[0] - r[0])) / d
        u = ((r[0] - p[0]) * (q[1] - p[1]) - (r[1] - p[1]) * (q[0] - p[0])) / d
        return 0 <= t <= 1 and 0 <= u <= 1

    if any(crosses(a, b, r, s) for r, s in edges):
        return 0.0

    def point_segment(p, r, s):
        dx, dy = s[0] - r[0], s[1] - r[1]
        length = dx * dx + dy * dy
        t = 0.0 if length == 0 else max(0.0, min(1.0, ((p[0] - r[0]) * dx + (p[1] - r[1]) * dy) / length))
        return math.hypot(p[0] - r[0] - t * dx, p[1] - r[1] - t * dy)

    return min([point_segment(p, a, b) for p in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
               + [point_segment(p, r, s) for p in (a, b) for r, s in edges])


def clear_of_others(path: list[tuple[float, float]], pads: tuple[PadInfo, ...]) -> bool:
    """Whether every leg keeps ESCAPE_GAP from the other-net pads of these pads' parts. KiCad's router cannot run a
    leg that close: it bends away, often diagonally across the pad's edge, and the pad entry check refuses the route."""
    boxes = [box for pad in pads for box in pad.others]
    return all(_segment_box_distance(a, b, box) >= ESCAPE_GAP for a, b in zip(path, path[1:]) for box in boxes)


# A click lands up to about 0.02 mm off where it was aimed (one screen point is 0.044 mm at 22.56 points per mm),
# so a leg meant to run along a pad's axis can end a few microns off it, and KiCad's router adds a tiny diagonal to
# get back on. The tool trues those up afterwards over IPC: within the misalignment, never further.
JOG = 0.05  # mm: a routed segment this short is a jog, not a route
TRUE_UP_REACH = 0.05  # mm: no corner moves further than this when a jog is taken out
DIRECTIONS = [(1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)]


def _heading(a, b):
    angle = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))
    return DIRECTIONS[round(angle / 45) % 8]


def _meet(p, d, q, e):
    """Where the line through p along d meets the line through q along e, or None if they are parallel."""
    cross = d[0] * e[1] - d[1] * e[0]
    if cross == 0:
        return None
    t = ((q[0] - p[0]) * e[1] - (q[1] - p[1]) * e[0]) / cross
    return (p[0] + t * d[0], p[1] + t * d[1])


def _chains(tracks: list, fixed: set) -> list:
    """The new tracks as chains of points between fixed points, junctions and loose ends: [(points, ids)]."""
    key = lambda point: (round(point[0], 6), round(point[1], 6))  # noqa: E731
    ends: dict = {}
    for track in tracks:
        for point in (track.start, track.end):
            ends.setdefault((track.layer, key(point)), []).append(track)
    stop = lambda layer, point: key(point) in fixed or len(ends[(layer, key(point))]) != 2  # noqa: E731
    seen, chains = set(), []
    for track in tracks:
        if track.id in seen:
            continue
        for first in (track.start, track.end):
            if stop(track.layer, first):
                break
        else:
            continue  # a closed loop of new copper: left alone
        points, ids, here, item = [first], [], first, track
        while item and item.id not in seen:
            seen.add(item.id)
            ids.append(item.id)
            here = item.end if key(item.start) == key(here) else item.start
            points.append(here)
            if stop(item.layer, here):
                break
            item = next((o for o in ends[(item.layer, key(here))] if o.id != item.id), None)
        chains.append((points, ids))
    return chains


def _project(point, heading, through):
    """The point on the line through `through` along `heading` nearest `point`."""
    length = math.hypot(*heading)
    ux, uy = heading[0] / length, heading[1] / length
    t = (point[0] - through[0]) * ux + (point[1] - through[1]) * uy
    return (through[0] + t * ux, through[1] + t * uy)


def _true_chain(points: list, free_start: bool = False, free_end: bool = False, force: bool = False,
                widths: list[float] | None = None):
    """The chain with its jogs taken out, every segment on a 45-degree heading and both ends where they were (a free
    end, a new via of this run's, may slide onto its line), as its corners and, for each new segment, the index of the
    leg it keeps (the longest on that heading); or None if it has no jog (unless forced) or cannot be trued with no
    leg's end, and no free end, moving more than TRUE_UP_REACH."""
    legs = list(zip(points, points[1:]))
    if not force and not any(math.dist(a, b) < JOG for a, b in legs):
        return None
    lines = []  # [heading, a point on it, its length, the index of that leg, width]
    for index, (a, b) in enumerate(legs):
        length = math.dist(a, b)
        if length < JOG:
            continue
        heading = _heading(a, b)
        width = widths[index] if widths is not None else None
        same_width = width is None or lines and math.isclose(lines[-1][4], width, abs_tol=1e-6)
        if lines and lines[-1][0] == heading and same_width:
            if length > lines[-1][2]:
                lines[-1][1:4] = [((a[0] + b[0]) / 2, (a[1] + b[1]) / 2), length, index]
            continue
        lines.append([heading, ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2), length, index, width])
    if not lines:
        return None
    if not free_start:
        lines[0][1] = points[0]  # the ends stay put: a pad centre, an older via, older copper
    if not free_end:
        lines[-1][1] = points[-1]
    # Keep collinear width-transition segments aligned on the anchored end's line (pad centre).
    for i in range(len(lines) - 1):
        if lines[i][0] == lines[i + 1][0]:
            if i == 0 and not free_start:
                lines[i + 1][1] = lines[i][1]
            elif i + 1 == len(lines) - 1 and not free_end:
                lines[i][1] = lines[i + 1][1]
            else:
                anchor = lines[i][1] if lines[i][2] >= lines[i + 1][2] else lines[i + 1][1]
                lines[i][1] = lines[i + 1][1] = anchor
    first = _project(points[0], lines[0][0], lines[0][1]) if free_start else points[0]
    last = _project(points[-1], lines[-1][0], lines[-1][1]) if free_end else points[-1]
    if max(math.dist(first, points[0]), math.dist(last, points[-1])) > TRUE_UP_REACH:
        return None
    corners = [first]
    for (d, p, _, idx_a, _), (e, q, *_) in zip(lines, lines[1:]):
        if d == (-e[0], -e[1]):
            return None
        if d == e:
            corners.append(_project(legs[idx_a][1], d, p))
        else:
            corners.append(_meet(p, d, q, e))
    corners.append(last)
    if len(lines) == 1 and compass_direction(first, last) != lines[0][0]:
        return None
    for a, b in zip(corners, corners[1:]):
        if math.dist(a, b) < JOG or _heading(a, b) == (0, 0):
            return None
    keep = [line[3] for line in lines]
    if any(max(math.dist(legs[k][0], corners[i]), math.dist(legs[k][1], corners[i + 1])) > TRUE_UP_REACH
           for i, k in enumerate(keep)):
        return None
    return corners, keep


def _point_to_polyline(p, points):
    return min(_distance_to_segment(p, a, b) for a, b in zip(points, points[1:]))


def _distance_to_segment(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = dx * dx + dy * dy
    t = 0.0 if length == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def true_up(added: list, before: list, pads: list, land: tuple | None = None,
            land_reach: float = 0.0) -> tuple[dict, list, float, dict]:
    """For the router's new tracks: {id: (start, end)} to move, ids to delete, the furthest anything moves, and
    {via id: point} for a new via that slides with the one new track ending on it. Pad centres, older vias and every
    older track's ends stay where they are. A loose new end within land_reach of `land` (the older track end a route
    was asked to end on, where KiCad's router stops at the pointer, not on the end) is brought exactly onto it."""
    key = lambda point: (round(point[0], 6), round(point[1], 6))  # noqa: E731
    fixed = {key((p.x, p.y)) for p in pads}
    for item in before:
        fixed.update((key(item.start), key(item.end)))
    tracks = [i for i in added if i.kind == "track"]
    ends = collections.Counter(key(point) for item in before + tracks if item.kind == "track" for point in (item.start, item.end))
    free = {}  # a new via with exactly one track on it, on no pad and no older copper, may slide with that track's end
    for via in (i for i in added if i.kind == "via"):
        if key(via.start) not in fixed and ends[key(via.start)] == 1:
            free[key(via.start)] = via.id
        fixed.add(key(via.start))
    by_id = {t.id: t for t in tracks}
    moves, deletes, furthest, via_moves = {}, [], 0.0, {}
    for points, ids in _chains(tracks, fixed):
        original = points
        landed = False
        if land:
            for index in (0, -1):
                end = points[index]
                if key(end) not in fixed and 1e-6 < math.dist(end, land) <= land_reach:
                    if math.dist(end, land) > TRUE_UP_REACH:
                        break  # further off than a true-up may move anything: left for the checks to judge
                    points = list(points)
                    points[index] = land
                    landed = True
        free_start, free_end = key(points[0]) in free, key(points[-1]) in free
        trued = _true_chain(points, free_start, free_end, force=landed, widths=[by_id[i].width for i in ids])
        if not trued:
            continue
        if landed:
            furthest = max(furthest, math.dist(original[0], points[0]), math.dist(original[-1], points[-1]))
        corners, keep = trued
        for flag, old, new in ((free_start, points[0], corners[0]), (free_end, points[-1], corners[-1])):
            if flag and math.dist(old, new) > 1e-6:
                via_moves[free[key(old)]] = tuple(round(v, 6) for v in new)
                furthest = max(furthest, math.dist(old, new))
        for i, k in enumerate(keep):
            furthest = max(furthest, math.dist(points[k], corners[i]), math.dist(points[k + 1], corners[i + 1]))
            moves[ids[k]] = (tuple(round(v, 6) for v in corners[i]), tuple(round(v, 6) for v in corners[i + 1]))
        deletes += [item_id for index, item_id in enumerate(ids) if index not in keep]
    return moves, deletes, furthest, via_moves


def pad_waypoints(start: PadInfo, target: PadInfo, waypoints: list, grid: float = 0.0) -> list:
    """Give the interactive router straight pad escapes, with bends outside the pads, and legs clear of the same
    parts' other pads."""
    points = list(waypoints)
    # An explicit endpoint click must not close the route before its straight approach.
    while points and points[0] == ("through", start.x, start.y):
        points.pop(0)
    while points and points[-1] == ("through", target.x, target.y):
        points.pop()

    # Plan the approach pair together. Two independent axis stubs can leave a
    # vertical middle between horizontal stubs, forcing right-angle corners.
    dx, dy = abs(target.x - start.x), abs(target.y - start.y)
    if not points and dx > 1e-6 and dy > 1e-6 and all(
            p.through or p.round or abs((p.angle + 45) % 90 - 45) < 1e-6 for p in (start, target)):
        def clearance(pad, axis):
            if pad.through or pad.round:
                return 0.0
            index = axis ^ (round(pad.angle / 90) % 2)
            return (pad.size or (2 * pad.half, 2 * pad.half))[index] / 2 + 0.3 + min(pad.half, 0.15) + grid

        sx, sy = clearance(start, 0), clearance(start, 1)
        tx, ty = clearance(target, 0), clearance(target, 1)
        candidates = []
        for diagonal, pair in (
            (min(dx - sx, dy - ty), "hv"), (min(dy - sy, dx - tx), "vh")):
            if diagonal > 0:
                local = [(dx - diagonal, 0), (dx, diagonal)] if pair == "hv" else [(0, dy - diagonal), (diagonal, dy)]
                candidates.append((diagonal, local))
        if dx - dy >= sx + tx:
            lead = max(sx, min((dx - dy) / 2, dx - dy - tx))
            candidates.append((dy, [(lead, 0), (lead + dy, dy)]))
        if dy - dx >= sy + ty:
            lead = max(sy, min((dy - dx) / 2, dy - dx - ty))
            candidates.append((dx, [(0, lead), (dx, lead + dx)]))
        # When facing pad edges leave a narrow corridor, bevel both ends of
        # the middle leg instead of forcing a right-angle turn at either pad.
        diagonal = (dx - sx - tx) / 2
        if 0 < diagonal < dy / 2:
            candidates.append((2 * diagonal, [(sx, 0), (sx + diagonal, diagonal),
                                               (sx + diagonal, dy - diagonal), (dx - tx, dy)]))
        diagonal = (dy - sy - ty) / 2
        if 0 < diagonal < dx / 2:
            candidates.append((2 * diagonal, [(0, sy), (diagonal, sy + diagonal),
                                               (dx - diagonal, sy + diagonal), (dx, dy - ty)]))
        sign_x, sign_y = math.copysign(1, target.x - start.x), math.copysign(1, target.y - start.y)
        on_board = lambda local: [("through", round(start.x + x * sign_x, 6), round(start.y + y * sign_y, 6))  # noqa: E731
                                  for x, y in local if math.hypot(x, y) > 1e-6 and math.hypot(x - dx, y - dy) > 1e-6]
        # A plan whose leg runs past the part's other pad cannot be routed as drawn: try the others.
        clear = [item for item in candidates if clear_of_others(
            [(start.x, start.y)] + [p[1:] for p in on_board(item[1])] + [(target.x, target.y)], (start, target))]
        if clear:
            axis = 0 if dx >= dy else 1
            _, local = max(clear, key=lambda item: (item[1][0][axis] > 0,
                           item[1][-1][1 - axis] == (dy if axis == 0 else dx), item[0]))
            return on_board(local)

    def escape(pad, toward):
        if pad.through or pad.round:
            return None
        theta = math.radians(pad.angle)
        axes = ((math.cos(theta), -math.sin(theta)), (math.sin(theta), math.cos(theta)))
        delta = (toward[0] - pad.x, toward[1] - pad.y)
        projections = [delta[0] * x + delta[1] * y for x, y in axes]
        if min(abs(v) for v in projections) < 1e-6:
            return None  # Already a straight approach, no extra corner needed.
        def along(axis):
            length = (pad.size or (2 * pad.half, 2 * pad.half))[axis] / 2 + 0.3 + min(pad.half, 0.15) + grid
            direction = math.copysign(length, projections[axis])
            return ("through", round(pad.x + axes[axis][0] * direction, 6), round(pad.y + axes[axis][1] * direction, 6))

        first = max(range(2), key=lambda i: abs(projections[i]))
        for axis in (first, 1 - first):  # out along the other axis if the first runs past the part's other pad
            point = along(axis)
            if clear_of_others([(pad.x, pad.y), point[1:]], (pad,)):
                return point
        return along(first)

    first = escape(start, points[0][1:3] if points else (target.x, target.y))
    last = escape(target, points[-1][1:3] if points else (start.x, start.y))
    return ([first] if first else []) + points + ([last] if last else [])


def check_pad_entries(pads: list[PadInfo], items: list[Item], before: list[Item]) -> None:
    """A snapped or obstacle-deflected route must not silently undo the planned approach."""
    unchanged = {item.id: item for item in before}
    for pad in pads:
        if pad.through or pad.round:
            continue
        centered = False
        copper = pad.copper or PadCopper((pad.x, pad.y), pad.size or (2 * pad.half, 2 * pad.half), pad.angle, "rect")
        for item in items:
            if item.kind != "track" or item.net != pad.net or item.layer not in pad.layers:
                continue
            centered |= any(math.hypot(end[0] - pad.x, end[1] - pad.y) <= 1e-6 for end in (item.start, item.end))
            if unchanged.get(item.id) == item:
                continue
            inside = [copper.contains_endcap(end, 0.0) for end in (item.start, item.end)]
            if not all(inside) and copper.intersects_segment(item.start, item.end):
                angle = math.degrees(math.atan2(item.end[1] - item.start[1], item.end[0] - item.start[0]))
                if abs((angle + pad.angle + 45) % 90 - 45) > 1.0:
                    raise RouteFailure("pad_entry", f"the router crossed {pad.name}'s edge diagonally despite its straight approach: "
                                       "something blocks the straight way out, so it went round. Give a --through point a little "
                                       "way out along the pad's axis, or a --via to pass under what blocks it")
        if not centered:
            raise RouteFailure("pad_entry", f"the route does not end at {pad.name}'s centre")


@dataclasses.dataclass
class Item:
    id: str
    kind: str  # "track" or "via"
    net: str
    layer: str | None
    start: tuple[float, float]
    end: tuple[float, float]
    width: float


def plan_layers(start: PadInfo, target: PadInfo, vias: int, asked: str | None, active: str) -> list[str]:
    """The copper layer of each leg, first to last. A via goes between the outer layers, the pair the router uses
    by default, so any other journey is refused before anything moves."""
    if asked and asked not in start.layers:
        raise RouteFailure("bad_request", f"{start.name} has no copper on {asked}: it is on {', '.join(start.layers)}")

    def legs(first: str) -> list[str]:
        out = [first]
        for _ in range(vias):
            if out[-1] not in OTHER_SIDE:
                raise RouteFailure("bad_request", f"a via from {out[-1]}: this tool only hops between F.Cu and B.Cu")
            out.append(OTHER_SIDE[out[-1]])
        return out

    if asked:
        candidates = [asked]
    elif not start.through:
        candidates = list(start.layers)
    else:
        candidates = [active] + [layer for layer in start.layers if layer != active]
    for first in candidates:
        if first in start.layers and (first in OTHER_SIDE or not vias):
            plan = legs(first)
            if plan[-1] in target.layers:
                return plan
    raise RouteFailure("bad_request", f"{vias} via(s) cannot take a route from {start.name} (on {start.layers}) "
                       f"to {target.name} (on {target.layers}) with the first leg on {candidates}")


PAD_REACH = 0.05  # mm: how near R15.1@X,Y must be to that pad's centre


def split_pad_spec(spec: str) -> tuple[str, tuple[float, float] | None]:
    """R15.1 or SW1.1@117.365,101.11: the pad's name and, when given, the point that says which pad of that number."""
    name, at, point = spec.partition("@")
    if "." not in name.strip(".") or not name.rpartition(".")[2]:
        raise RouteFailure("bad_request", f"{spec}: a pad is written like R15.1, or SW1.1@X,Y for one of several pads numbered 1")
    if not at:
        return name, None
    try:
        return name, point_arg(point)
    except argparse.ArgumentTypeError as exc:
        raise RouteFailure("bad_request", str(exc)) from None


def choose_pad(name: str, near: tuple[float, float] | None, found: list[PadInfo]) -> PadInfo:
    """The one pad of this number: the only one, or the one at the point given. Several, with no point, is refused."""
    where = ", ".join(f"({pad.x}, {pad.y})" for pad in found)
    if near is None:
        if len(found) > 1:
            raise RouteFailure("bad_request", f"{name} names {len(found)} pads, at {where}: say which, like {name}@{found[0].x},{found[0].y}")
        return found[0]
    best = min(found, key=lambda pad: math.dist((pad.x, pad.y), near))
    if math.dist((best.x, best.y), near) > PAD_REACH:
        raise RouteFailure("bad_request", f"no pad {name} within {PAD_REACH} mm of ({near[0]}, {near[1]}): it is at {where}")
    return best


VIA_DIAMETER = 0.6  # mm: default through-via diameter for clearance pre-checks
VIA_CLEARANCE = 0.2  # mm: the board's clearance


def check_via_spot(at: tuple[float, float], start: PadInfo, items: list, pads: list) -> None:
    """Refuse, before anything moves, a new via on a pad, on other copper or too near it. Keep-outs are not read."""
    where = f"({at[0]}, {at[1]})"
    for name, net, (x0, y0, x1, y1) in pads:
        gap = math.hypot(max(x0 - at[0], 0, at[0] - x1), max(y0 - at[1], 0, at[1] - y1))
        if gap == 0:
            raise RouteFailure("bad_request", f"a new via at {where} would sit inside {name}'s pad")
        if net != start.net and gap < VIA_DIAMETER / 2 + VIA_CLEARANCE:
            raise RouteFailure("bad_request", f"a new via at {where} would be {gap:.3f} mm from {name} ({net or 'no net'}), "
                               f"closer than {VIA_DIAMETER / 2 + VIA_CLEARANCE:.2f} mm")
    for item in items:
        gap = _distance_to_segment(at, item.start, item.end) - item.width / 2 - VIA_DIAMETER / 2
        if item.net == start.net and gap < 0:
            raise RouteFailure("bad_request", f"{where} is on copper of {start.net} already: end there with @X,Y instead")
        if item.net != start.net and gap < VIA_CLEARANCE:
            raise RouteFailure("bad_request", f"a new via at {where} would be {max(gap, 0):.3f} mm from a {item.net} "
                               f"{item.kind}, closer than {VIA_CLEARANCE} mm")


END_REACH = 0.005  # mm: how near @X,Y must be to a via's centre or a track's end


def copper_end(items: list[Item], at: tuple[float, float], start: PadInfo) -> PadInfo:
    """The via or track end of the start pad's net at this point, as a target to route to. Anything else is refused:
    another net, the middle of a track (no T junctions) or nothing."""
    where = f"({at[0]}, {at[1]})"
    here = [item for item in items if (item.kind == "via" and math.dist(item.start, at) <= END_REACH)
            or (item.kind == "track" and min(math.dist(item.start, at), math.dist(item.end, at)) <= END_REACH)]
    ours = [item for item in here if item.net == start.net]
    if not ours:
        if here:
            raise RouteFailure("bad_request", f"the copper at {where} is on {here[0].net}, not {start.net}")
        crossing = [item for item in items if item.kind == "track" and _near_segment(at, item.start, item.end, item.width / 2)]
        if crossing:
            raise RouteFailure("bad_request", f"{where} is on a {crossing[0].net} track but not at its end: "
                               "a route may end only on a via's centre or a track's end")
        raise RouteFailure("bad_request", f"no via centre or track end within {END_REACH} mm of {where}")
    via = next((item for item in ours if item.kind == "via"), None)
    if via:
        return PadInfo(f"the via at ({via.start[0]}, {via.start[1]})", start.net, via.start[0], via.start[1],
                       ("F.Cu", "B.Cu"), via.width / 2, True, round=True)
    end = min((point for item in ours for point in (item.start, item.end)), key=lambda point: math.dist(point, at))
    away = [compass_direction(end, item.end if math.dist(item.start, end) <= END_REACH else item.start) for item in ours]
    if len(ours) > 2 or (len(ours) == 2 and away[0] != tuple(-v for v in away[1] or (0, 0))):
        # If a guide point lies near one of the meeting tracks, the armed router snaps onto
        # that track and clicks short before any copper reaches the corner.
        raise RouteFailure("bad_request", f"{where} is a corner where {len(ours)} {start.net} tracks meet: the router "
                           "snaps onto them on the way in. End on a loose track end, a via, or where two tracks meet "
                           "in a straight line")
    layers = tuple(sorted({item.layer for item in ours}))
    return PadInfo(f"the track end at ({end[0]}, {end[1]})", start.net, end[0], end[1], layers,
                   min(item.width for item in ours) / 2, False, round=True, track_end=True)


def removed_copper(before: list[Item], after: list[Item], net: str) -> list[str]:
    """This net's vias and tracks from before that no copper after covers: what KiCad took away while routing."""
    lost = []
    tracks = [item for item in after if item.kind == "track" and item.net == net]
    vias = {item.start for item in after if item.kind == "via" and item.net == net}
    for item in before:
        if item.net != net:
            continue
        if item.kind == "via":
            if not any(math.dist(item.start, at) <= 1e-4 for at in vias):
                lost.append(f"the via at ({item.start[0]}, {item.start[1]})")
        elif not any(other.layer == item.layer and _near_segment(item.start, other.start, other.end, 1e-4)
                     and _near_segment(item.end, other.start, other.end, 1e-4) for other in tracks):
            lost.append(f"the {item.layer} track ({item.start[0]}, {item.start[1]}) to ({item.end[0]}, {item.end[1]})")
    return lost


def check_nets(start: PadInfo, target: PadInfo, net: str | None) -> None:
    if not start.net:
        raise RouteFailure("wrong_net", f"{start.name} has no net")
    if start.net != target.net:
        raise RouteFailure("wrong_net", f"{start.name} is on {start.net} and {target.name} is on {target.net or 'no net'}")
    if net and net != start.net:
        raise RouteFailure("wrong_net", f"{start.name} and {target.name} are on {start.net}, not {net}")


def _near_segment(p: tuple[float, float], a: tuple[float, float], b: tuple[float, float], reach: float) -> bool:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = dx * dx + dy * dy
    t = 0.0 if length == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy) <= reach


def connected(start: PadInfo, target: PadInfo, items: list[Item]) -> bool:
    """Whether copper of their net joins the two pads: tracks that end on a pad, on another track on the same
    layer, or on a via. A pour does not count: this is about the route."""
    def on_pad(item: Item, pad: PadInfo) -> bool:
        if item.kind == "via":
            return math.hypot(item.start[0] - pad.x, item.start[1] - pad.y) <= pad.half
        return item.layer in pad.layers and any(
            math.hypot(end[0] - pad.x, end[1] - pad.y) <= pad.half for end in (item.start, item.end))

    def touch(a: Item, b: Item) -> bool:
        if a.kind == "track" and b.kind == "track" and a.layer != b.layer:
            return False
        reach = max(a.width, b.width) / 2
        return (any(_near_segment(end, b.start, b.end, reach) for end in (a.start, a.end))
                or any(_near_segment(end, a.start, a.end, reach) for end in (b.start, b.end)))

    net = [item for item in items if item.net == start.net]
    frontier = [item for item in net if on_pad(item, start)]
    seen = {item.id for item in frontier}
    while frontier:
        item = frontier.pop()
        if on_pad(item, target):
            return True
        for other in net:
            if other.id not in seen and touch(item, other):
                seen.add(other.id)
                frontier.append(other)
    return False


def gap_words(start: PadInfo, target: PadInfo, items: list[Item]) -> str:
    """Where the copper from each end stops, nearest the other: so a caller knows which stretch the router could not
    lay, and can add a --through corner or a --via there."""
    net = [item for item in items if item.net == start.net]

    def island(pad):
        reach = [item for item in net if (item.kind == "via" and math.dist(item.start, (pad.x, pad.y)) <= pad.half)
                 or (item.kind == "track" and min(math.dist(item.start, (pad.x, pad.y)), math.dist(item.end, (pad.x, pad.y))) <= pad.half)]
        seen = {item.id for item in reach}
        frontier = list(reach)
        while frontier:
            item = frontier.pop()
            for other in net:
                if other.id not in seen and any(math.dist(e, f) <= 1e-6 for e in (item.start, item.end) for f in (other.start, other.end)):
                    seen.add(other.id)
                    frontier.append(other)
        return [point for item in net if item.id in seen for point in (item.start, item.end)] or [(pad.x, pad.y)]

    ends = [(math.dist(a, b), a, b) for a in island(start) for b in island(target)]
    gap, a, b = min(ends)
    return (f": the copper from {start.name} stops at ({a[0]:.3f}, {a[1]:.3f}) and from {target.name} at "
            f"({b[0]:.3f}, {b[1]:.3f}), {gap:.2f} mm apart; the router found no way between them. Add a --through corner "
            "or a --via in that stretch")


def diff_items(before: list[Item], after: list[Item]) -> dict:
    was, now = {i.id: i for i in before}, {i.id: i for i in after}
    added = [now[i] for i in now if i not in was]
    return {"segments_added": sum(i.kind == "track" for i in added), "vias_added": sum(i.kind == "via" for i in added),
            "items_removed": sum(i not in now for i in was), "added": added}


def new_drc(before: dict, after: dict, box: tuple[float, float, float, float], margin: float = 2.0) -> dict:
    """From two `kicad-cli pcb drc --format json` reports: violations that are new and within `margin` mm of the
    route's box, and the change in unconnected items."""
    def key(violation: dict) -> tuple:
        places = tuple(sorted((i.get("pos", {}).get("x"), i.get("pos", {}).get("y")) for i in violation.get("items", [])))
        return violation.get("type"), places

    def near(violation: dict) -> bool:
        return any(box[0] - margin <= i.get("pos", {}).get("x", 1e9) <= box[2] + margin
                   and box[1] - margin <= i.get("pos", {}).get("y", 1e9) <= box[3] + margin
                   for i in violation.get("items", []))

    known = {key(v) for v in before.get("violations", [])}
    new = [v for v in after.get("violations", []) if key(v) not in known and near(v)]
    return {"new_errors_nearby": [{"type": v.get("type"), "description": v.get("description"),
                                   "at": [(i.get("pos", {}).get("x"), i.get("pos", {}).get("y")) for i in v.get("items", [])]}
                                  for v in new],
            "unconnected": [len(before.get("unconnected_items", [])), len(after.get("unconnected_items", []))]}


# The route itself, against any editor and board: on the Mac the two classes below, in the tests stand-ins.

@dataclasses.dataclass
class Request:
    start: str
    target: str
    waypoints: list[tuple[str, float, float]]  # ("via" or "through", x, y), in the order written
    layer: str | None = None
    net: str | None = None
    save: bool = False
    drc: bool = False
    hover_offset: tuple[float, float] | None = None  # for proving the on-screen net check: hover this far off the pad
    mode: str | None = None  # "shove", or None for the mode the editor is in
    width: float | None = None  # track width in mm, or None for the router's current/netclass width


class Run:
    def __init__(self, editor, board, request: Request):
        self.editor, self.board, self.request = editor, board, request
        self.mapping: Mapping | None = None
        self.grid = 0.0
        self.mode = None  # as the router's panel last said
        self.mode_changes: list[str] = []  # modes this run set, in order
        self.width = None
        self.width_changes: list[float] = []  # track widths this run set, in order
        self.sessions = 0
        self.true_ups = 0  # exact IPC edits this run made, each one undo step
        self.waypoints = []
        self.failure_picture = None  # taken at the moment of failure: leaving the router clears what it showed
        self.net_readings: list = []  # panel readings that matched only as the nearest of the nets near the pointer
        self.pad_boxes: list | None = None  # every copper pad, read once at the first arming
        self.via_point: tuple[float, float] | None = None  # via@X,Y: the route ends on a new via here

    def frame(self, points: list[tuple[float, float]], pads: list[PadInfo]) -> None:
        """A mapping for a view that shows every point: the one in use if it does, otherwise a framed one."""
        safe = self.editor.safe_rect()
        smallest = min(pad.half for pad in pads)
        status = self.editor.read_status()
        self.grid = status.grid or 0.0
        if not self.grid:
            raise RouteFailure("mapping_failed", "the status bar shows no grid")
        cached = self.editor.load_view(status)
        if cached and fits(cached, points, safe, smallest):
            self.mapping = cached
            return
        mapping = self.calibrate()
        if not fits(mapping, points, safe, smallest):
            xs, ys = [p[0] for p in points], [p[1] for p in points]
            middle = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
            if not on_rect(mapping.to_screen(*middle), safe):
                self.editor.menu("View", "Zoom to Fit")
                self.editor.wait(0.4)
                mapping = self.calibrate()
            for _ in range(8):
                # KiCad centers the view on the pointer: hop toward the connection until it is on the canvas.
                point = mapping.to_screen(*middle)
                self.editor.read_at(min(max(point[0], safe[0]), safe[2]), min(max(point[1], safe[1]), safe[3]))
                self.editor.key("f4")
                self.editor.wait(0.3)
                mapping = self.calibrate()
                if fits(mapping, points, safe, smallest):
                    break
                if not on_rect(mapping.to_screen(*middle), safe):
                    continue
                steps = zoom_steps(mapping, points, safe)
                if not steps:
                    break
                self.editor.scroll(*mapping.to_screen(*middle), steps)
                self.editor.wait(0.4)
                mapping = self.calibrate()
            if not fits(mapping, points, safe, smallest, closely=False):
                raise RouteFailure("mapping_failed", f"no view shows all of this connection at a usable zoom "
                                   f"({mapping.scale:.1f} points per mm): add waypoints and route it in parts")
        self.mapping = mapping
        self.editor.save_view(self.editor.read_status(), mapping)

    def calibrate(self) -> Mapping:
        left, top, right, bottom = self.editor.safe_rect()
        width, height = right - left, bottom - top
        spots = [(left + 0.15 * width, top + 0.15 * height), (left + 0.85 * width, top + 0.2 * height),
                 (left + 0.45 * width, top + 0.85 * height)]
        for attempt in (1, 2):
            samples = [(sx, sy, self.editor.read_at(sx, sy)) for sx, sy in spots]
            self.grid = samples[-1][2].grid or self.grid
            try:
                return fit_mapping(samples, self.grid)
            except RouteFailure:
                if attempt == 2:
                    raise
        raise AssertionError("unreachable")

    def pad_point(self, pad: PadInfo) -> tuple[float, float]:
        """Where to put the pointer so that it is on this pad, sharpening the mapping when it is too coarse. Only
        with the router not armed: once it is, the status bar snaps to whatever is under the pointer."""
        assert self.mapping
        mapping = self.mapping
        if mapping.error > 0.8 * pad.half:
            mapping = refine_near(self.editor, mapping, pad.x, pad.y, self.grid)
        return mapping.to_screen(pad.x, pad.y)

    def hover(self, x: float, y: float, sx: float, sy: float, what: str, tolerance: float) -> Status:
        """Move there and prove it from the status bar. A wrong reading stops the run: no click follows."""
        reading = self.editor.read_at(sx, sy)
        if not within(reading, x, y, tolerance):
            self.editor.wait(0.25)
            reading = self.editor.read_at(sx, sy)
        if not within(reading, x, y, tolerance):
            raise RouteFailure("mapping_failed", f"pointer sent to {what} ({x:.3f}, {y:.3f}) but the status bar says "
                               f"({reading.x:.3f}, {reading.y:.3f}); more than {tolerance:.3f} mm off, so nothing was clicked")
        return reading

    def start_router(self, x: float, y: float, sx: float, sy: float, what: str, net: str, tolerance: float) -> None:
        baseline = self.editor.read_strip()
        self.hover(x, y, sx, sy, what, tolerance)
        # The copper the router can arm on, read now: while it runs KiCad answers nothing over IPC.
        near = []
        if hasattr(self.board, "pad_boxes"):
            if self.pad_boxes is None:
                self.pad_boxes = self.board.pad_boxes()  # pads do not move during a run; tracks and vias may be new
            near = near_nets((x, y), self.board.items(), self.pad_boxes, tolerance + NEAR_MARGIN)
        self.editor.key("x")
        panel = None
        for _ in range(4):
            self.editor.wait(0.2)
            panel = self.editor.read_panel()
            if panel.routing:
                break
        if not panel.routing:
            words = refusal_text(baseline, self.editor.read_strip())
            self.failure_picture = self.editor.picture(None, f"{what}-refused")
            self.editor.forget_view()  # the info bar can move the canvas
            raise RouteFailure("refused", words or "the router did not start, and said nothing on the canvas", refusal=words)
        names = self.board.net_names() if hasattr(self.board, "net_names") else []
        ok, nearest = read_net(panel.net or "", net, near, names)
        if not ok:
            raise RouteFailure("wrong_net", f"at {what} the router would route {panel.net}, not {net}"
                               + (f"; the nets near the pointer nearest that reading: {', '.join(nearest)}" if nearest else "")
                               + (f"; copper within {tolerance + NEAR_MARGIN:.2f} mm: {', '.join(near)}" if near else ""),
                               shown=panel.net, near=near)
        if nearest:
            self.net_readings.append({"shown": panel.net, "taken_as": net, "nearest": nearest, "near": near})
        self.mode = panel.mode or self.mode
        wanted = MODES[self.request.mode]
        if not same_mode(panel.mode, wanted):
            # The editor keeps whatever mode it was left in, saved setting included: set the wanted one and arm again.
            if self.sessions or self.mode_changes:
                raise RouteFailure("mode_not_set", f"asked for {wanted}, but the router says {panel.mode}")
            self.editor.leave_router()
            self.board.wait_ready()
            self.editor.set_router_mode(wanted)
            self.mode_changes.append(wanted)
            self.start_router(x, y, sx, sy, what, net, tolerance)
            return
        if self.request.width is not None:
            panel = self.apply_width(self.request.width, panel)
        self.width = panel.width or self.width
        self.sessions += 1

    def apply_width(self, target_width: float, panel: Panel | None = None) -> Panel:
        """Set the armed interactive router's track width through Q and verify it from the message panel."""
        if panel is None:
            panel = self.editor.read_panel()
        if panel.width is not None and abs(panel.width - target_width) <= 0.001:
            self.width = panel.width
            return panel
        self.editor.set_track_width(target_width)
        self.width_changes.append(target_width)
        for _ in range(4):
            self.editor.wait(0.2)
            panel = self.editor.read_panel()
            if panel.routing and panel.width is not None and abs(panel.width - target_width) <= 0.001:
                break
        if not panel.routing or panel.width is None or abs(panel.width - target_width) > 0.001:
            self.width = panel.width or self.width
            raise RouteFailure("width_not_set", f"asked for {target_width:.3f} mm, but the router says {panel.width}")
        self.width = panel.width
        return panel

    def walk(self, start: PadInfo, target: PadInfo, layers: list[str]) -> None:
        assert self.mapping
        net = start.net
        known = {item.id for item in self.board.items()}  # while the router runs, KiCad answers nothing over IPC
        hold = self.grid / 2 + self.mapping.error  # how far from the aim the status bar may read, before any snap
        moving = max(hold + 0.6, 1.0)  # with the router armed it may also snap to something near the pointer
        self.board.set_active_layer(layers[0])
        sx, sy = self.pad_point(start)
        end_x, end_y = self.pad_point(target)
        offset = self.request.hover_offset
        if offset:
            sx, sy = self.mapping.to_screen(start.x + offset[0], start.y + offset[1])
            self.start_router(start.x + offset[0], start.y + offset[1], sx, sy, f"{start.name} (offset)", net, hold)
        else:
            self.start_router(start.x, start.y, sx, sy, start.name, net, hold)
        leg = 0
        for wp in self.waypoints:
            kind, x, y = wp[0], wp[1], wp[2]
            sx, sy = self.mapping.to_screen(x, y)
            self.hover(x, y, sx, sy, f"{kind} point", moving)
            if kind == "width_at":
                self.editor.click(sx, sy)
                self.editor.wait(0.15)
                panel = self.editor.read_panel()
                if not panel.routing:
                    return
                self.apply_width(wp[3], panel)
                continue
            if kind == "through":
                self.editor.click(sx, sy)
                self.editor.wait(0.15)
                if not self.editor.read_panel().routing:
                    # A corner near a pad snaps onto it and ends the route: another click would start a new one.
                    return
                if not self.request.waypoints and not self.via_point:
                    # Start the last leg at its pad too. Finishing onto a pad
                    # lets PNS snap diagonally across its edge or close early.
                    self.editor.leave_router()
                    self.board.wait_ready()
                    ends = [point for item in self.board.items() if item.id not in known
                            and item.kind == "track" and item.net == net for point in (item.start, item.end)]
                    if not ends:
                        raise RouteFailure("not_closed", "the router did not commit the first pad escape")
                    join = min(ends, key=lambda point: math.dist(point, (x, y)))
                    if math.dist(join, (x, y)) > moving:
                        raise RouteFailure("not_closed", f"the first pad escape did not reach its guide point ({x:.3f}, {y:.3f}): "
                                           f"its copper stops at ({join[0]:.3f}, {join[1]:.3f}), {math.dist(join, (x, y)):.2f} mm "
                                           "short. Give --through corners along a way that is clear, nearer the pad")
                    tx, ty = self.pad_point(target)
                    self.start_router(target.x, target.y, tx, ty, target.name, net, hold)
                    if len(self.waypoints) > 1:
                        _, ax, ay = self.waypoints[-1]
                        px, py = self.mapping.to_screen(ax, ay)
                        self.hover(ax, ay, px, py, "second pad escape", moving)
                        self.editor.click(px, py)
                        self.editor.wait(0.15)
                        if not self.editor.read_panel().routing:
                            return
                    target = dataclasses.replace(target, name="the new track end", x=join[0], y=join[1])
                    end_x, end_y = self.mapping.to_screen(*join)
                    break
                continue
            self.editor.key("v")
            self.editor.wait(0.2)
            self.hover(x, y, sx, sy, "via point", moving)
            self.editor.click(sx, sy)
            self.editor.wait(0.3)
            leg += 1
            if self.via_point and (kind, x, y) == self.waypoints[-1]:
                return  # the new via is the end: run() leaves the router, and Esc drops any leg begun past it
            if not self.editor.read_panel().routing:
                # On the Mac the via's click ends the route: go on from the via, on the other side.
                self.editor.leave_router()
                self.board.wait_ready()
                now = self.board.items()
                missed = self.via_miss([item for item in now if item.id not in known])
                if missed:
                    self.failure_picture = self.editor.picture(None, "via-missed")
                    raise RouteFailure("via_missed", missed)
                known.update(item.id for item in now)
                self.board.set_active_layer(layers[leg])
                self.start_router(x, y, sx, sy, "the via", net, moving)
        reading = self.hover(target.x, target.y, end_x, end_y, target.name, moving)
        # A pad or via pulls the armed pointer onto its centre; a track end does not, so there the reading may be off
        # by up to half the track's width, and the true-up brings the last microns exactly onto the end.
        if not within(reading, target.x, target.y, target.half if target.track_end else 0.002):
            raise RouteFailure("mapping_failed", f"the router did not snap to {target.name} at ({target.x:.3f}, {target.y:.3f}): "
                               f"the status bar says ({reading.x:.3f}, {reading.y:.3f}), so the pointer is not on the pad; nothing was clicked there")
        baseline = self.editor.read_strip()
        self.editor.click(end_x, end_y)
        self.editor.wait(0.35)
        panel = self.editor.read_panel()
        if panel.routing:
            words = refusal_text(baseline, self.editor.read_strip())
            self.failure_picture = self.editor.picture(None, f"{target.name}-not-closed")
            raise RouteFailure("not_closed", words or f"the router could not finish on {target.name}", refusal=words)

    def via_miss(self, items: list[Item]) -> str | None:
        """A via lands where the route's front end got to, not under the pointer. Off by a grid step or a clearance is
        normal; further from every point asked for means the router got stuck on the way. A via may reach the board
        some legs after it was asked for, so each is compared with all of them."""
        asked = [(wp[1], wp[2]) for wp in self.request.waypoints if wp[0] == "via"] + ([self.via_point] if self.via_point else [])
        for via in (item for item in items if item.kind == "via"):
            off = min(math.hypot(via.start[0] - x, via.start[1] - y) for x, y in asked) if asked else 0.0
            if off > max(self.grid, 0.5) + 0.1:
                return (f"a via landed at ({via.start[0]:.2f}, {via.start[1]:.2f}), {off:.1f} mm from the nearest point asked for: "
                        "the router got stuck there. Add --through corners around what blocks it, or ask for a via it can reach")
        return None

    def apply_true_up(self, after: list[Item], moves: dict, deletes: list, via_moves: dict | None = None) -> list[Item]:
        """Take the jogs out over IPC, one undo step, and prove it from a fresh read: the moved tracks end exactly
        where asked, the deleted ones are gone, and nothing else changed."""
        self.true_ups += 1
        via_moves = via_moves or {}
        self.board.true_up(moves, deletes, via_moves)
        now = self.board.items()
        was, got = {i.id: i for i in after}, {i.id: i for i in now}
        wrong = [i for i in deletes if i in got]
        for item_id, ends in moves.items():
            item = got.get(item_id)
            if not item or not all(math.dist(a, b) <= 2e-6 for a, b in zip(sorted((item.start, item.end)), sorted(ends))):
                wrong.append(item_id)
        for item_id, point in via_moves.items():
            item = got.get(item_id)
            if not item or math.dist(item.start, point) > 2e-6:
                wrong.append(item_id)
        wrong += [i for i in was if i not in moves and i not in deletes and i not in via_moves and got.get(i) != was[i]]
        wrong += [i for i in got if i not in was]
        if wrong:
            raise RouteFailure("true_up_failed", f"the exact edit did not read back as asked: {wrong[:4]}")
        return now

    def undo_to(self, before: list[Item]) -> bool:
        """Back to exactly the items the run started from, with the editor's undo, one step at a time."""
        wanted = {item.id: item for item in before}
        for _ in range(len(self.waypoints) + self.sessions + self.true_ups + 2):
            if {item.id: item for item in self.board.items()} == wanted:
                return True
            self.editor.key("z", command=True)
            self.editor.wait(0.4)
            self.board.wait_ready()
        return {item.id: item for item in self.board.items()} == wanted

    def run(self) -> dict:
        began = time.time()
        request = self.request
        result: dict = {"from": request.start, "to": request.target, "ok": False, "closed": False, "status": None,
                        "segments_added": 0, "vias_added": 0, "items_removed": 0, "refusal": None, "drc": None,
                        "saved": False, "picture": None}
        added: list[Item] = []
        before: list[Item] | None = None
        pristine_text = None
        touched = False
        save_attempted = False
        try:
            start = self.board.pad(request.start)
            on_copper = request.target.startswith("@")
            if request.target.startswith("via@"):
                try:
                    point = point_arg(request.target[4:])
                except argparse.ArgumentTypeError as exc:
                    raise RouteFailure("bad_request", str(exc)) from None
                check_via_spot(point, start, self.board.items(), self.board.pad_boxes())
                target = PadInfo(f"a new via at ({point[0]}, {point[1]})", start.net, point[0], point[1],
                                 ("F.Cu", "B.Cu"), VIA_DIAMETER / 2, True, round=True)
                self.via_point = point
            elif on_copper:
                try:
                    point = point_arg(request.target[1:])
                except argparse.ArgumentTypeError as exc:
                    raise RouteFailure("bad_request", str(exc)) from None
                target = copper_end(self.board.items(), point, start)
                result["to_copper"] = target.name
            else:
                target = self.board.pad(request.target)
            check_nets(start, target, request.net)
            vias = sum(wp[0] == "via" for wp in request.waypoints) + bool(self.via_point)
            layers = plan_layers(start, target, vias, request.layer, self.board.active_layer())
            result.update(net=start.net, layers=layers)
            before = self.board.items()
            if connected(start, target, before):
                raise RouteFailure("already_connected", f"{request.start} and {request.target} are already joined by copper")
            quality_before = self.board.quality()
            if request.drc:
                pristine_text = self.board.text()
            touched = True
            self.editor.activate()
            self.board.clear_selection()
            self.waypoints = pad_waypoints(start, target, request.waypoints, self.editor.read_status().grid or 0.0)
            if not request.waypoints and self.waypoints:
                path = [(start.x, start.y)] + [point[1:] for point in self.waypoints] + [(target.x, target.y)]
                # Steer within each bevel and let PNS form its exact corners.
                # Clicking the corner itself can fix a subpixel reverse jog.
                bevels = [(a, b) for a, b in zip(path, path[1:])
                          if abs(a[0] - b[0]) > 1e-6 and abs(a[1] - b[1]) > 1e-6]
                self.waypoints = []
                for index, (a, b) in enumerate(bevels):
                    fraction = 0.5 if len(bevels) == 1 else 0.9 if index == 0 else 0.1
                    self.waypoints.append(("through", a[0] + fraction * (b[0] - a[0]),
                                           a[1] + fraction * (b[1] - a[1])))
            if self.via_point:
                self.waypoints.append(("via", *self.via_point))  # the route ends where this via lands
            points = [(start.x, start.y), (target.x, target.y)] + [(wp[1], wp[2]) for wp in self.waypoints]
            self.frame(points, [start, target])
            try:
                self.walk(start, target, layers)
            finally:
                try:
                    self.editor.leave_router()
                    self.board.wait_ready()
                finally:
                    if MODES[request.mode] != MODES[None]:  # the editor keeps a mode, and saves it, until it is changed back
                        self.editor.set_router_mode(MODES[None])
                        self.mode_changes.append(MODES[None])
            after = self.board.items()
            change = diff_items(before, after)
            added = change.pop("added")
            result.update(change)
            # The router may set a via off the point asked for, to keep its clearance.
            result["vias_at"] = [[round(c, 3) for c in item.start] for item in added if item.kind == "via"]
            if self.via_point:
                placed = [item for item in added if item.kind == "via" and item.net == start.net]
                if not placed:
                    raise RouteFailure("not_closed", f"no via of {start.net} was placed near {self.via_point}")
                via = min(placed, key=lambda item: math.dist(item.start, self.via_point))
                target = dataclasses.replace(target, name=f"the new via at ({via.start[0]}, {via.start[1]})",
                                             x=via.start[0], y=via.start[1])
                result["plane"] = ("whether the via reaches a plane is not read here: run the design check after "
                                   "refilling the zones")
            result["closed"] = connected(start, target, after)
            if not result["closed"]:
                missed = self.via_miss(added)
                if missed:
                    raise RouteFailure("via_missed", missed)
                raise RouteFailure("not_closed", f"the router finished, but no copper joins {request.start} to {request.target}"
                                   + gap_words(start, target, after))
            land = (target.x, target.y) if target.track_end else None
            moves, deletes, furthest, via_moves = true_up(added, before, [start] if self.via_point else [start, target],
                                                          land, target.half)
            if moves or deletes:
                after = self.apply_true_up(after, moves, deletes, via_moves)
                result["trued_up"] = {"moved": len(moves), "deleted": len(deletes), "vias_moved": len(via_moves),
                                      "furthest_mm": round(furthest, 4)}
                if self.via_point and via_moves:
                    moved = next(i for i in after if i.id in via_moves)
                    target = dataclasses.replace(target, x=moved.start[0], y=moved.start[1])
                    result["vias_at"] = [[round(c, 3) for c in i.start] for i in after if i.kind == "via" and i.id not in {b.id for b in before}]
                change = diff_items(before, after)
                added = change.pop("added")
                result.update(change)
                if not connected(start, target, after):
                    raise RouteFailure("true_up_failed", "after truing up the jogs, no copper joins the two ends")
            if on_copper:
                lost = removed_copper(before, after, start.net)
                if lost:
                    raise RouteFailure("removed_copper", "KiCad removed existing copper while routing: " + "; ".join(lost[:4]))
            check_pad_entries([start, target], after, before)
            try:
                quality_after = self.board.quality()
            except Exception as exc:
                raise RouteFailure("route_quality_failed", f"could not check the routed copper: {exc}") from exc
            new_quality = [message for key, message in quality_after.items() if key not in quality_before]
            new_warnings = [m for m in new_quality if m.startswith("warning: ")]
            new_errors = [m for m in new_quality if not m.startswith("warning: ")]
            if new_errors:
                self.failure_picture = self.editor.picture(self.mapping_box(added) if added else None, "route-quality")
                hint = ("; a --through corner a hair off a pad's axis leaves tiny jogs like these: put it well off the axis, "
                        "so the router bevels the corner, or leave it out" if request.waypoints and any("micro_segment" in m for m in new_errors) else "")
                raise RouteFailure("route_quality", "; ".join(new_errors[:3]) + hint)
            result.update(ok=True, status="closed", width_mm=self.width,
                          scale_points_per_mm=round(self.mapping.scale, 2))
            if new_warnings:
                result["warnings"] = new_warnings
            if request.drc:
                result["drc"] = self.board.drc(pristine_text, self.board.text(), box_of(added or []))
            if request.save:
                save_attempted = True
                self.board.save()
                self.editor.wait(0.3)
                result["saved"] = self.editor.title_clean()
                if not result["saved"]:
                    result.update(ok=False, status="save_failed", saved=None, disk_state="unverified")
        except Exception as exc:
            failure = exc if isinstance(exc, RouteFailure) else RouteFailure(
                "operation_failed", f"{type(exc).__name__}: {exc}")
            result.update(ok=False, status=failure.status, reason=failure.reason)
            result["refusal"] = failure.extra.get("refusal", result["refusal"])
            if "shown" in failure.extra:
                result["shown"] = failure.extra["shown"]
            if "near" in failure.extra:
                result["near_nets"] = failure.extra["near"]
            if save_attempted:
                result.update(saved=None, disk_state="unverified")
                result["reason"] += "; save was attempted: verify the saved file and editor before continuing"
            elif before is not None and touched:
                try:
                    result["undone"] = self.undo_to(before)
                except Exception as exc:
                    result["undone"] = False
                    result["reason"] += f"; rollback could not be verified: {exc}"
                if not result["undone"]:
                    result["reason"] += "; UNDO NOT VERIFIED: look at the editor"
                    try:
                        remaining = self.board.items()
                        change = diff_items(before, remaining)
                        change.pop("added")
                        result.update(change, closed=connected(start, target, remaining))
                    except Exception:
                        result.update(closed=None, board_state="unverified")
                else:
                    result.update(closed=False, segments_added=0, vias_added=0, items_removed=0)
        if touched:
            box = None
            if added and self.mapping:
                box = self.mapping_box(added)
            try:
                result["picture"] = self.failure_picture or self.editor.picture(box, f"{request.start}-{request.target}")
            except Exception as exc:
                result.update(ok=False, picture_error=str(exc))
                if result["status"] == "closed":
                    result["status"] = "picture_failed"
        result.update(router_mode=self.mode, mode_changes=self.mode_changes)
        if self.width_changes:
            result["width_changes"] = self.width_changes
        if self.width is not None and "width_mm" not in result:
            result["width_mm"] = self.width
        if self.net_readings:
            result["net_readings"] = self.net_readings
        result["seconds"] = round(time.time() - began, 1)
        return result

    def mapping_box(self, items: list[Item]) -> tuple[float, float, float, float]:
        assert self.mapping
        corners = [self.mapping.to_screen(*end) for item in items for end in (item.start, item.end)]
        return (min(c[0] for c in corners), min(c[1] for c in corners),
                max(c[0] for c in corners), max(c[1] for c in corners))


def box_of(items: list[Item]) -> tuple[float, float, float, float]:
    ends = [end for item in items for end in (item.start, item.end)] or [(0.0, 0.0)]
    return min(e[0] for e in ends), min(e[1] for e in ends), max(e[0] for e in ends), max(e[1] for e in ends)


# The Mac: the real editor and the real board.

HELPER = r'''import Foundation
import CoreGraphics
import ImageIO
import Vision

// top | move X Y | click X Y | scroll X Y STEPS | ocr FILE X Y W H | crop FILE OUT X Y W H
// Fractions of the picture for ocr and crop. With BOARD_GRAPH_WINDOW set, input goes only
// to that window: exit 3 otherwise.
let a = CommandLine.arguments
let source = CGEventSource(stateID: .hidSystemState)
func topWindow() -> Int {
    let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] ?? []
    let panels = ProcessInfo.processInfo.environment["BOARD_GRAPH_ROUTER_PANELS"] == "1"
    for w in list {
        let layer = w[kCGWindowLayer as String] as? Int
        if layer == 0 || (panels && layer == 8) { return w[kCGWindowNumber as String] as? Int ?? 0 }
    }
    return 0
}
func guardWindow() {
    let environment = ProcessInfo.processInfo.environment
    if let want = environment["BOARD_GRAPH_WINDOW"], Int(want) != topWindow() {
        print("the top window is #\(topWindow()), not #\(want)")
        exit(3)
    }
}
func point(_ i: Int) -> CGPoint { CGPoint(x: Double(a[i])!, y: Double(a[i + 1])!) }
func post(_ type: CGEventType, _ at: CGPoint, clicks: Int64 = 0) {
    let event = CGEvent(mouseEventSource: source, mouseType: type, mouseCursorPosition: at, mouseButton: .left)!
    if clicks > 0 { event.setIntegerValueField(.mouseEventClickState, value: clicks) }
    event.flags = []
    event.post(tap: .cghidEventTap)
    usleep(30_000)
}
func image(_ path: String, _ i: Int) -> CGImage {
    let picture = CGImageSourceCreateImageAtIndex(CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil)!, 0, nil)!
    let w = Double(picture.width), h = Double(picture.height)
    return picture.cropping(to: CGRect(x: Double(a[i])! * w, y: Double(a[i + 1])! * h, width: Double(a[i + 2])! * w, height: Double(a[i + 3])! * h))!
}
switch a[1] {
case "top":
    print(topWindow())
case "move":
    guardWindow()
    post(.mouseMoved, point(2))
    post(.mouseMoved, point(2))
case "click":
    guardWindow()
    post(.mouseMoved, point(2))
    post(.leftMouseDown, point(2), clicks: 1)
    post(.leftMouseUp, point(2), clicks: 1)
case "scroll":
    guardWindow()
    let steps = Int32(a[4])!
    post(.mouseMoved, point(2))
    usleep(100_000)
    for _ in 0..<abs(steps) {
        CGEvent(scrollWheelEvent2Source: source, units: .line, wheelCount: 1, wheel1: steps > 0 ? 1 : -1,
                wheel2: 0, wheel3: 0)!.post(tap: .cghidEventTap)
        usleep(40_000)
    }
case "ocr", "ocr-accurate":
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = (a[1] == "ocr-accurate") ? .accurate : .fast
    request.usesLanguageCorrection = false
    try VNImageRequestHandler(cgImage: image(a[2], 3), options: [:]).perform([request])
    let lines = (request.results ?? []).sorted { o, p in
        abs(o.boundingBox.midY - p.boundingBox.midY) < 0.2 ? o.boundingBox.minX < p.boundingBox.minX : o.boundingBox.midY > p.boundingBox.midY
    }
    for line in lines { print(line.topCandidates(1)[0].string) }
case "crop":
    let out = CGImageDestinationCreateWithURL(URL(fileURLWithPath: a[3]) as CFURL, "public.png" as CFString, 1, nil)!
    CGImageDestinationAddImage(out, image(a[2], 4), nil)
    CGImageDestinationFinalize(out)
default:
    exit(64)
}
'''


def helper_path() -> str:
    digest = hashlib.sha256(HELPER.encode()).hexdigest()[:12]
    folder = pathlib.Path(os.path.expanduser(CACHE))
    binary = folder / f"route-helper-{digest}"
    if not binary.exists():
        folder.mkdir(parents=True, exist_ok=True)
        source = folder / f"route-helper-{digest}.swift"
        source.write_text(HELPER)
        subprocess.run(["swiftc", "-O", "-o", str(binary), str(source)], check=True, capture_output=True, timeout=300)
    return str(binary)


class MacEditor:
    """One PCB Editor window on the Mac's screen."""

    def __init__(self, window: dict, panels: bool = False):
        self.window = window
        self.panels = panels
        self.pid = str(window["pid"])
        self.number = str(window["number"])
        self.left, self.top, self.width, self.height = window["bounds"]
        self.helper = helper_path()
        self.shot = f"/tmp/board-graph-route-{self.pid}.png"
        self.view_file = pathlib.Path(os.path.expanduser(CACHE)) / f"route-view-{self.pid}.json"

    def _run(self, *args: str, guarded: bool = True) -> str:
        env = dict(os.environ, BOARD_GRAPH_WINDOW=self.number) if guarded else None
        if env is not None and self.panels:
            env["BOARD_GRAPH_ROUTER_PANELS"] = "1"
        done = subprocess.run([self.helper, *args], capture_output=True, text=True, env=env, check=False)
        if done.returncode == 3:
            raise RouteFailure("lost_focus", f"{done.stdout.strip()}: nothing was sent. Another session or a dialog may have the screen")
        if done.returncode:
            raise RouteFailure("mapping_failed", f"helper {args[0]} failed: {done.stderr.strip()}")
        return done.stdout

    def wait(self, seconds: float) -> None:
        time.sleep(seconds)

    def activate(self) -> None:
        mac.osascript(f'tell application "System Events" to set frontmost of {mac.process(self.pid)} to true')
        for _ in range(20):
            if self._run("top", guarded=False).strip() == self.number:
                time.sleep(0.15)
                return
            time.sleep(0.1)
        raise RouteFailure("lost_focus", f"window #{self.number} did not come to the front")

    def safe_rect(self) -> tuple[float, float, float, float]:
        return (self.left + SAFE[0] * self.width, self.top + SAFE[1] * self.height,
                self.left + SAFE[2] * self.width, self.top + SAFE[3] * self.height)

    def _ocr(self, x: float, y: float, w: float, h: float, *, accurate: bool = False) -> list[str]:
        done = subprocess.run(["screencapture", "-x", "-o", "-l", self.number, self.shot], capture_output=True, check=False)
        if done.returncode:
            raise RouteFailure("mapping_failed", f"could not capture the window: {done.stderr.decode(errors='replace').strip()}")
        verb = "ocr-accurate" if accurate else "ocr"
        return [line for line in self._run(verb, self.shot, f"{x:.5f}", f"{y:.5f}", f"{w:.5f}", f"{h:.5f}", guarded=False).splitlines() if line]

    def read_status(self) -> Status:
        said = ""
        for _ in range(3):
            said = " ".join(self._ocr(0.0, 1 - STATUS_ROW / self.height, 1.0, STATUS_ROW / self.height))
            status = parse_status(said)
            if status:
                return status
            time.sleep(0.1)
        raise RouteFailure("mapping_failed", f"the status bar did not read as a position in mm: {said!r}")

    def read_at(self, sx: float, sy: float) -> Status:
        self._run("move", f"{sx:.1f}", f"{sy:.1f}")
        time.sleep(0.25)
        return self.read_status()

    def read_panel(self) -> Panel:
        top, bottom = PANEL_ROW[1] / self.height, PANEL_ROW[0] / self.height
        return parse_panel(" ".join(self._ocr(0.0, 1 - top, 0.75, top - bottom, accurate=True)))

    def read_strip(self) -> list[str]:
        return self._ocr(STRIP[0], STRIP[1], STRIP[2] - STRIP[0], STRIP[3] - STRIP[1])

    def key(self, name: str, command: bool = False) -> None:
        """Through System Events, like mac.py: after a Command key posted from the helper, KiCad ignored its plain keys."""
        if self._run("top", guarded=False).strip() != self.number:
            raise RouteFailure("lost_focus", f"window #{self.number} is not on top: nothing was sent. Another session or a dialog may have the screen")
        modifier = " using {command down}" if command else ""
        mac.osascript(f'tell application "System Events" to key code {KEY_CODES[name]}{modifier}')

    def click(self, sx: float, sy: float) -> None:
        self._run("click", f"{sx:.1f}", f"{sy:.1f}")

    def scroll(self, sx: float, sy: float, steps: int) -> None:
        self._run("scroll", f"{sx:.1f}", f"{sy:.1f}", str(steps))

    def menu(self, top: str, item: str) -> None:
        mac.osascript(f'tell application "System Events" to tell {mac.process(self.pid)}\n  {mac.menu_line([top, item])}\nend tell\n')

    def set_router_mode(self, name: str) -> None:
        """Through Route > Interactive Router Settings: on the Mac no key changes the mode."""
        process = mac.process(self.pid)
        mac.osascript(f'tell application "System Events" to tell {process}\n'
                      '  click (first menu item of menu "Route" of menu bar 1 whose name starts with "Interactive Router Settings")\nend tell\n')
        time.sleep(1.0)
        mac.osascript(f'tell application "System Events" to tell {process}\n'
                      f'  click radio button "{name}" of group "Mode" of window 1\n'
                      '  click button "OK" of window 1\nend tell\n')
        time.sleep(0.5)
        self.activate()

    def set_track_width(self, width_mm: float) -> None:
        """Through Track and Via Dimensions (Q) while the interactive router is armed."""
        self.key("q")
        time.sleep(0.6)
        process = mac.process(self.pid)
        names = "{" + ", ".join(mac.quote(title) for title in DIMENSIONS) + "}"
        script = (
            f'tell application "System Events" to tell {process}\n'
            '  if not frontmost then error "PCB Editor lost focus"\n'
            '  set dlg to missing value\n'
            '  if (count of sheets of front window) > 0 then\n'
            f'    if name of sheet 1 of front window is in {names} then set dlg to sheet 1 of front window\n'
            '  end if\n'
            '  if dlg is missing value then\n'
            f'    if name of front window is in {names} then set dlg to front window\n'
            '  end if\n'
            '  if dlg is missing value then error "Track and Via Dimensions dialog did not open"\n'
            '  set focusedItem to value of attribute "AXFocusedUIElement"\n'
            '  if focusedItem is missing value or value of attribute "AXRole" of focusedItem is not in {"AXTextField", "AXComboBox"} then\n'
            '    if (count of text fields of dlg) > 0 then\n'
            '      set focused of text field 1 of dlg to true\n'
            '    else if (count of combo boxes of dlg) > 0 then\n'
            '      set focused of combo box 1 of dlg to true\n'
            '    else\n'
            '      error "Track width field not found in Track and Via Dimensions"\n'
            '    end if\n'
            '  end if\n'
            '  keystroke "a" using {command down}\n'
            f'  keystroke "{width_mm:.6f} mm"\n'
            '  key code 36\n'
            'end tell\n'
        )
        try:
            mac.osascript(script)
        except mac.MacError as error:
            raise RouteFailure("width_not_set", f"could not set track width to {width_mm:.3f} mm: {error}") from None
        time.sleep(0.4)
        self.activate()

    def leave_router(self) -> None:
        self.key("esc")
        time.sleep(0.12)
        self.key("esc")
        time.sleep(0.15)

    def load_view(self, status: Status) -> Mapping | None:
        try:
            saved = json.loads(self.view_file.read_text())
        except (OSError, ValueError):
            return None
        if saved.get("zoom") != status.zoom or saved.get("grid") != status.grid or saved.get("bounds") != self.window["bounds"]:
            return None
        return Mapping(**saved["mapping"])

    def save_view(self, status: Status, mapping: Mapping) -> None:
        self.view_file.write_text(json.dumps({"zoom": status.zoom, "grid": status.grid, "bounds": self.window["bounds"],
                                              "mapping": dataclasses.asdict(mapping)}))

    def forget_view(self) -> None:
        self.view_file.unlink(missing_ok=True)

    def title_clean(self) -> bool:
        rows = [w for w in mac.windows(every=True) if str(w["number"]) == self.number]
        return bool(rows) and not rows[0]["title"].startswith("*")

    def picture(self, box: tuple[float, float, float, float] | None, name: str) -> str:
        folder = pathlib.Path("/tmp/board-graph-route")
        folder.mkdir(exist_ok=True)
        out = folder / f"{time.strftime('%H%M%S')}-{re.sub(r'[^A-Za-z0-9.-]', '_', name)}.png"
        subprocess.run(["screencapture", "-x", "-o", "-l", self.number, self.shot], capture_output=True, check=False)
        if box is None:
            x, y, w, h = 0.0, 0.0, 1.0, 1.0
        else:
            margin = 120
            x = max(0.0, (box[0] - margin - self.left) / self.width)
            y = max(0.0, (box[1] - margin - self.top) / self.height)
            w = min(1.0 - x, (box[2] - box[0] + 2 * margin) / self.width)
            h = min(1.0 - y, (box[3] - box[1] + 2 * margin) / self.height)
        self._run("crop", self.shot, str(out), f"{x:.5f}", f"{y:.5f}", f"{w:.5f}", f"{h:.5f}", guarded=False)
        return str(out)


def pad_box(pad) -> tuple[float, float, float, float]:
    """A kipy pad's copper as an upright box around its rotated, offset outline, in mm."""
    return kicad_ipc.pad_copper_box(pad)


class KipyBoard(kicad_ipc.KipyBoardCacheMixin):
    """The open board, over IPC: an editor from `mac.py open` answers on its own socket."""

    def __init__(self, pid: str):
        from kipy import KiCad  # the venv only; the tests never reach this
        from kipy.util.board_layer import canonical_name, layer_from_canonical_name, is_copper_layer
        self.canonical_name, self.layer_id, self.is_copper = canonical_name, layer_from_canonical_name, is_copper_layer
        own = f"/tmp/kicad/api-{pid}.sock"
        self.kicad = KiCad(socket_path=f"ipc://{own}" if os.path.exists(own) else None, timeout_ms=60000)
        self.board = self.kicad.get_board()
        self.name = pathlib.PurePosixPath(self.board.document.board_filename).stem
        self.project = self.board.document.project.path
        self.init_ipc_caches()

    def pad(self, spec: str) -> PadInfo:
        name, near = split_pad_spec(spec)
        reference, _, number = name.rpartition(".")
        fps = self.footprints_for_ref(reference)
        if not fps:
            raise RouteFailure("bad_request", f"no footprint {reference} on {self.name}")
        found = [
            self._pad_info(spec, fp, pad)
            for fp in fps
            for pad in fp.definition.pads
            if pad.number == number
        ]
        if not found:
            raise RouteFailure("bad_request", f"{reference} has no pad {number}")
        return choose_pad(name, near, found)

    def _pad_info(self, spec: str, footprint, pad) -> PadInfo:
        info = kicad_ipc.extract_pad_geometry(pad, self.canonical_name, self.copper_here)
        layers = info["layers"] if info else ()
        center = info["center"] if info else (pad.position.x / 1e6, pad.position.y / 1e6)
        px, py = info["position"] if info else (pad.position.x / 1e6, pad.position.y / 1e6)
        size_mm = info["size"] if info else (0.0, 0.0)
        angle_deg = info["angle_deg"] if info else float(pad.padstack.angle.degrees)
        shape_name = info["shape"] if info else "unknown"
        round_ratio = info["round_ratio"] if info else 0.0
        net_name = info["net"] if info else pad.net.name
        others = tuple(pad_box(other) for other in footprint.definition.pads
                       if other.net.name != pad.net.name
                       and any(self.is_copper(layer) and self.canonical_name(layer) in layers
                               for layer in other.padstack.layers))
        return PadInfo(spec, net_name, px, py, layers,
                       min(size_mm) / 2.0, len(layers) > 1,
                       size_mm, angle_deg,
                       shape_name == "circle",
                       PadCopper(center, size_mm, angle_deg, shape_name, round_ratio),
                       others)

    def pad_boxes(self) -> list:
        """(name, net, box) for every copper pad on the board."""
        out = []
        for footprint in self.all_footprints():
            reference = footprint.reference_field.text.value
            for pad in footprint.definition.pads:
                if any(self.copper_here(layer) for layer in pad.padstack.layers):
                    out.append((f"{reference}.{pad.number}", pad.net.name, pad_box(pad)))
        return out

    def copper_here(self, layer) -> bool:
        """A copper layer this board has. A through-hole pad lists every copper layer KiCad knows (In1.Cu to In30.Cu),
        not only the board's four."""
        return self.is_copper(layer) and layer in self.enabled_layer_ids()

    def net_names(self) -> list[str]:
        return list(self.net_map())

    def active_layer(self) -> str:
        return self.canonical_name(self.board.get_active_layer())

    def set_active_layer(self, name: str) -> None:
        self.wait_ready()
        self.board.set_active_layer(self.layer_id(name))

    def items(self) -> list[Item]:
        self.wait_ready()
        out = []
        for track in self.board.get_tracks():
            out.append(Item(str(track.id.value), "track", track.net.name, self.canonical_name(track.layer),
                            (track.start.x / 1e6, track.start.y / 1e6), (track.end.x / 1e6, track.end.y / 1e6), track.width / 1e6))
        for via in self.board.get_vias():
            at = (via.position.x / 1e6, via.position.y / 1e6)
            out.append(Item(str(via.id.value), "via", via.net.name, None, at, at, via.diameter / 1e6))
        return out

    def wait_ready(self) -> None:
        """The editor answers IPC only when no tool holds it."""
        for _ in range(30):
            try:
                self.board.get_active_layer()
                return
            except Exception as error:  # kipy raises ApiError subclasses of Exception
                if "busy" not in str(error).lower():
                    raise RouteFailure("ipc_failed", f"IPC failed: {error}") from error
                time.sleep(0.1)
        raise RouteFailure("ipc_failed", "KiCad stayed busy: a tool or dialog is holding it")

    def clear_selection(self) -> None:
        self.board.clear_selection()

    def true_up(self, moves: dict, deletes: list, via_moves: dict | None = None) -> None:
        """Move these tracks' ends and new vias and delete these tracks in one commit: one undo takes it back."""
        from kipy.geometry import Vector2
        self.wait_ready()
        tracks = {str(track.id.value): track for track in self.board.get_tracks()}
        changed = []
        vias = {str(via.id.value): via for via in self.board.get_vias()}
        for item_id, point in (via_moves or {}).items():
            vias[item_id].position = Vector2.from_xy(round(point[0] * 1e6), round(point[1] * 1e6))
            changed.append(vias[item_id])
        for item_id, (start, end) in moves.items():
            track = tracks[item_id]
            track.start = Vector2.from_xy(round(start[0] * 1e6), round(start[1] * 1e6))
            track.end = Vector2.from_xy(round(end[0] * 1e6), round(end[1] * 1e6))
            changed.append(track)
        commit = self.board.begin_commit()
        try:
            if changed:
                self.board.update_items(changed)
            if deletes:
                self.board.remove_items([tracks[item_id] for item_id in deletes])
        except Exception:
            self.board.drop_commit(commit)
            raise
        self.board.push_commit(commit, "Route tool: true up the router's jogs")
        self.invalidate_copper_cache()

    def save(self) -> None:
        self.board.save()

    def text(self) -> str:
        return self.board.get_as_string()

    def quality(self) -> dict:
        findings, _ = audit_live_board(pathlib.Path(self.project), self.board)
        result = {}
        for item in findings:
            if item.severity in {"error", "warning"}:
                identity = (item.severity, item.rule, item.net, item.layer, item.point_mm, item.message)
                occurrence = 0
                while (identity, occurrence) in result:
                    occurrence += 1
                result[identity, occurrence] = f"{item.severity}: {item.rule} at {item.point_mm}: {item.message}"
        return result

    def drc(self, before: str, after: str, box: tuple[float, float, float, float]) -> dict:
        """DRC of the board as it is in the editor, unsaved, on copies beside the project's rules."""
        project = pathlib.Path(self.project)
        cli = kicad_ipc.invoke_kicad_cli_path()
        reports = []
        with tempfile.TemporaryDirectory(prefix="board-graph-route-drc-") as folder:
            for suffix in (".kicad_pro", ".kicad_dru"):
                source = project / f"{self.name}{suffix}"
                if source.exists():
                    shutil.copy(source, pathlib.Path(folder) / source.name)
            for label, board_text in (("before", before), ("after", after)):
                path = pathlib.Path(folder) / f"{self.name}.kicad_pcb"
                path.write_text(board_text)
                out = pathlib.Path(folder) / f"{label}.json"
                done = subprocess.run([sys.executable, str(cli), "pcb", "drc", "--format", "json", "--refill-zones", "--output", str(out), str(path)],
                                      capture_output=True, text=True, check=False, timeout=600)
                if not out.exists():
                    return {"error": (done.stderr or done.stdout).strip()[-300:]}
                reports.append(json.loads(out.read_text()))
        return new_drc(reports[0], reports[1], box)


def pick_editor(pid: str | None) -> dict:
    editors = [w for w in mac.windows() if w["app"] == "PCB Editor"]
    if pid:
        editors = [w for w in editors if str(w["pid"]) == pid]
    if len(editors) != 1:
        listing = "; ".join(f'pid {w["pid"]}: {w["title"]}' for w in editors) or "none"
        raise RouteFailure("bad_request", f"{len(editors)} PCB Editor windows to choose from ({listing}): name one with --pid")
    return editors[0]


def point_arg(text: str) -> tuple[float, float]:
    try:
        x, y = (float(part) for part in text.split(","))
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text}: a board position is x,y in mm") from None
    return x, y


def width_arg(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text}: track width must be a number in mm") from None
    if not math.isfinite(value) or not (0.0 < value <= 100.0):
        raise argparse.ArgumentTypeError(f"{text}: track width must be > 0 and <= 100 mm")
    return value


def width_at_arg(text: str) -> tuple[float, float, float]:
    parts = text.split(",")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError(f"{text}: expected X,Y,MM in mm")
    x, y = point_arg(f"{parts[0]},{parts[1]}")
    return x, y, width_arg(parts[2])


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Route one connection with KiCad's real router on the Mac.")
    parser.add_argument("start", help="start pad, like R15.1; SW1.1@X,Y picks one of several pads numbered 1, and a "
                        "plain SW1.1 on such a part is refused with their positions")
    parser.add_argument("target", help="target pad, written like the start pad; or @X,Y: a via's centre or a track's end "
                        "of the start pad's net, in mm (not the middle of a track), closed when the pad reaches that "
                        "copper; or via@X,Y: a new via there, refused on a pad or other copper (keep-outs are not "
                        "read), closed when the pad reaches the via; a design check after a refill tells whether it "
                        "reaches a plane")

    class Waypoint(argparse.Action):
        def __call__(self, _parser, namespace, value, _option=None):
            kind = "via" if self.dest == "via" else "width_at" if self.dest == "width_at" else "through"
            namespace.waypoints.append((kind, *value))

    parser.set_defaults(waypoints=[])
    parser.add_argument("--via", action=Waypoint, type=point_arg, metavar="X,Y", help="drop a via here and carry on, on the other side")
    parser.add_argument("--through", action=Waypoint, type=point_arg, metavar="X,Y", help="fix a corner here")
    parser.add_argument("--width-at", dest="width_at", action=Waypoint, type=width_at_arg, metavar="X,Y,MM",
                        help="click at X,Y and switch the armed router's track width to MM mm for the remaining route")
    parser.add_argument("--layer", help="copper layer of the first leg, like F.Cu")
    parser.add_argument("--mode", choices=["shove"], help="route in Shove, which pushes other nets' tracks aside, then go back to Walk Around; "
                        "without it every route is in Walk Around, whatever mode the editor was in")
    parser.add_argument("--width", type=width_arg, metavar="MM", help="route at this starting track width in mm, set through "
                        "Track and Via Dimensions (Q) and verified from the message panel")
    parser.add_argument("--net", help="stop unless the pads are on this net")
    parser.add_argument("--pid", help="the PCB Editor to use, when several are open")
    parser.add_argument("--save", action="store_true", help="save the board once the connection closed")
    parser.add_argument("--drc", action="store_true", help="DRC before and after, on copies: slower, adds new errors nearby")
    parser.add_argument("--test-hover-offset", type=point_arg, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def run_on_mac(args: argparse.Namespace) -> dict:
    try:
        window = pick_editor(args.pid)
        editor, board = MacEditor(window), KipyBoard(str(window["pid"]))
        if not window["title"].lstrip("*").startswith(f"{board.name} "):
            raise RouteFailure("bad_request", f'IPC answers for {board.name}, but the window says "{window["title"]}"')
    except RouteFailure as failure:
        return {"ok": False, "closed": False, "status": failure.status, "reason": failure.reason}
    request = Request(args.start, args.target, args.waypoints, args.layer, args.net, args.save, args.drc,
                      args.test_hover_offset, args.mode, args.width)
    result = Run(editor, board, request).run()
    result["board"] = board.name
    return result


def run_from_elsewhere(argv: list[str]) -> dict:
    """Carry this script, mac.py and the site settings it reads to the Mac once per version, run it there, and bring
    its picture back. The copy there reads the settings carried with it (BOARD_GRAPH_SITE names them)."""
    here = pathlib.Path(__file__).resolve().parent
    sources = {name: (here / name).read_text(encoding="utf-8")
               for name in ("kicad_route.py", "mac.py", "kicad_route_quality.py", "site_settings.py")}
    sources[SITE_COPY] = mac.SITE.file.read_text(encoding="utf-8-sig")
    digest = hashlib.sha256("".join(sources.values()).encode()).hexdigest()[:12]
    folder = f"{CACHE}/route-{digest}"
    upload = f'mkdir -p {folder}\n[ -f {folder}/.ok ] || {{\n'
    for name, source in sources.items():
        upload += f"cat > {folder}/{name} <<'BOARDGRAPHROUTESOURCE'\n{source}\nBOARDGRAPHROUTESOURCE\n"
    upload += f"touch {folder}/.ok\n}}\n"
    mac.bash(upload)
    done = mac.ssh(f"{site_settings.ENV}={folder}/{SITE_COPY} {VENV_PYTHON} {folder}/kicad_route.py "
                   f"{' '.join(shlex.quote(a) for a in argv)}", timeout=600)
    said = mac.text(done.stdout).strip()
    try:
        result = json.loads(said.splitlines()[-1])
    except (ValueError, IndexError):
        return {"ok": False, "closed": False, "status": "tool_failed",
                "reason": (said + " " + mac.text(done.stderr)).strip()[-600:]}
    if result.get("picture"):
        copy = mac.ssh(f"cat {shlex.quote(result['picture'])}")
        if copy.returncode == 0 and copy.stdout:
            local = mac.STATE / "route" / pathlib.PurePosixPath(result["picture"]).name
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(copy.stdout)
            result["picture"] = str(local)
    return result


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    args = parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        result = run_on_mac(args) if mac.LOCAL else run_from_elsewhere(argv)
    except mac.MacError as error:
        result = {"ok": False, "closed": False, "status": "mac_failed", "reason": str(error)}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        mac.report_routing_handoff()
