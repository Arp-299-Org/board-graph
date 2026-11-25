"""Read-only pad exit measurements and offline preflight checks for centered SMD pad exits."""
from __future__ import annotations

import math
import struct
import zlib

import kicad_rules_geom as rg

# Millimetres and degrees. These limits apply only to tasks requesting centered exits.
OFFSET_MM = 0.02
ANGLE_DEG = 1.0
EPS = 1e-8
SUPPORTED_SHAPES = ("rect", "roundrect", "oval")


def _pad_sdf(x, y, half_width, half_length, corner_radius=0.0):
    """Signed distance to a rounded rectangle: <= 0 inside/on boundary, > 0 outside."""
    iw = half_width - corner_radius
    ih = half_length - corner_radius
    qx = abs(x) - iw
    qy = abs(y) - ih
    return math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - corner_radius


def _clip_interval(a, b, half_width, half_length, corner_radius=0.0):
    """Parameter interval [lo, hi] in [0, 1] inside the closed rectangle or rounded rectangle."""
    lo, hi = 0.0, 1.0
    dx, dy = b[0] - a[0], b[1] - a[1]
    for origin, delta, half in ((a[0], dx, half_width), (a[1], dy, half_length)):
        if abs(delta) <= EPS:
            if abs(origin) > half + EPS:
                return None
        else:
            first, last = sorted(((-half - origin) / delta, (half - origin) / delta))
            lo, hi = max(lo, first), min(hi, last)
    if lo > hi + EPS:
        return None
    if lo > hi:
        lo = hi = (lo + hi) / 2
    if corner_radius <= EPS:
        return lo, hi
    iw = half_width - corner_radius
    ih = half_length - corner_radius
    aa = dx * dx + dy * dy
    t_eps = max(EPS, EPS / math.sqrt(aa)) if aa > EPS * EPS else EPS
    for sx in (-1, 1):
        for sy in (-1, 1):
            q_lo, q_hi = lo, hi
            for origin, delta, sign, inner in ((a[0], dx, sx, iw), (a[1], dy, sy, ih)):
                s_delta = sign * delta
                s_origin = sign * origin
                if abs(s_delta) <= EPS:
                    if s_origin < inner - EPS:
                        q_lo = q_hi + 1.0
                        break
                elif s_delta > 0:
                    q_lo = max(q_lo, (inner - s_origin) / s_delta)
                else:
                    q_hi = min(q_hi, (inner - s_origin) / s_delta)
            if q_lo > q_hi + t_eps:
                continue
            cx, cy = sx * iw, sy * ih
            ox, oy = a[0] - cx, a[1] - cy
            if aa <= EPS * EPS:
                if math.hypot(ox, oy) > corner_radius + EPS:
                    return None
                continue
            t_mid = -(ox * dx + oy * dy) / aa
            px, py = ox + t_mid * dx, oy + t_mid * dy
            dist2 = px * px + py * py
            if dist2 > (corner_radius + EPS) ** 2:
                return None
            half_chord = math.sqrt(max(0.0, corner_radius * corner_radius - dist2)) / math.sqrt(aa)
            c_lo, c_hi = t_mid - half_chord, t_mid + half_chord
            if q_lo <= lo + t_eps:
                lo = max(lo, c_lo)
            if q_hi >= hi - t_eps:
                hi = min(hi, c_hi)
            if lo > hi + t_eps:
                return None
            if lo > hi:
                lo = hi = (lo + hi) / 2
    return lo, hi


def inside_segment(a, b, half_width, half_length, corner_radius=0.0):
    """Clip a centerline to the closed pad outline, including a single point on the edge."""
    span = _clip_interval(a, b, half_width, half_length, corner_radius)
    if span is None:
        return None
    lo, hi = span
    dx, dy = b[0] - a[0], b[1] - a[1]
    return (a[0] + lo * dx, a[1] + lo * dy), (a[0] + hi * dx, a[1] + hi * dy)


def _classify_crossing(point, direction, half_width, half_length, corner_radius=0.0):
    x, y = point
    dx, dy = direction
    iw = half_width - corner_radius
    ih = half_length - corner_radius
    tol = max(EPS, 1e-6)
    if abs(y - half_length) <= tol and abs(x) <= iw + tol and dy > 0:
        return {"point_mm": point, "facing_edge": True, "offset_mm": abs(x),
                "angle_deg": math.degrees(math.atan2(abs(dx), abs(dy)))}
    if abs(y + half_length) <= tol and abs(x) <= iw + tol and dy < 0:
        return {"point_mm": point, "facing_edge": False, "offset_mm": abs(x),
                "angle_deg": math.degrees(math.atan2(abs(dx), abs(dy)))}
    if abs(abs(x) - half_width) <= tol and abs(y) <= ih + tol and dx * (1 if x >= 0 else -1) > 0:
        return {"point_mm": point, "facing_edge": False, "offset_mm": abs(y),
                "angle_deg": math.degrees(math.atan2(abs(dy), abs(dx)))}
    # On a full-radius rounded end (e.g. ratio == 0.5 or oval), allow exits on the facing arc within OFFSET_MM.
    if corner_radius > EPS and y > ih - tol and abs(x) <= OFFSET_MM + EPS and dy > 0:
        return {"point_mm": point, "facing_edge": True, "offset_mm": abs(x),
                "angle_deg": math.degrees(math.atan2(abs(dx), abs(dy)))}
    sx = 1 if x >= 0 else -1
    sy = 1 if y >= 0 else -1
    if corner_radius > EPS:
        nx, ny = (x - sx * iw) / corner_radius, (y - sy * ih) / corner_radius
        norm = math.hypot(nx, ny)
        dnorm = math.hypot(dx, dy)
        if norm > EPS and dnorm > EPS:
            cos_a = max(-1.0, min(1.0, (dx * nx + dy * ny) / (norm * dnorm)))
            angle = math.degrees(math.acos(cos_a))
        else:
            angle = 0.0
        offset = abs(x) if abs(ny) >= abs(nx) else abs(y)
    else:
        angle = (math.degrees(math.atan2(abs(dx), abs(dy))) if abs(dy) >= abs(dx)
                 else math.degrees(math.atan2(abs(dy), abs(dx))))
        offset = abs(x) if abs(y) >= abs(x) else abs(y)
    return {"point_mm": point, "facing_edge": False, "offset_mm": offset, "angle_deg": angle}


def touches_point(segment, point):
    a, b = segment
    dx, dy = b[0] - a[0], b[1] - a[1]
    length2 = dx*dx + dy*dy
    t = max(0.0, min(1.0, ((point[0]-a[0])*dx + (point[1]-a[1])*dy) / length2)) if length2 else 0
    return math.dist(point, (a[0] + t*dx, a[1] + t*dy)) <= EPS


def centerlines_meet(first, second):
    if any(touches_point(first, p) for p in second) or any(touches_point(second, p) for p in first):
        return True
    def side(seg, p):
        a, b = seg
        return (b[0]-a[0])*(p[1]-a[1]) - (b[1]-a[1])*(p[0]-a[0])
    return (side(first, second[0]) * side(first, second[1]) < 0 and
            side(second, first[0]) * side(second, first[1]) < 0)


def reaches_crossing(inside, centers, crossing):
    # Traverse only the track centerlines inside this pad. The pad copper itself
    # can electrically join disconnected stubs and must never bridge a gap here.
    pending = [seg for seg in inside if any(touches_point(seg, p) for p in centers)]
    remaining = set(inside).difference(pending)
    while pending:
        seg = pending.pop()
        if touches_point(seg, crossing):
            return True
        joined = {other for other in remaining if centerlines_meet(seg, other)}
        pending.extend(joined)
        remaining.difference_update(joined)
    return False


def crossing_report(half_width, half_length, segments, corner_radius=0.0):
    """Local coordinates: the facing short edge runs from (-w, h) to (w, h)."""
    crossings = []
    issues = []
    inside_tracks = []
    for a, b, _width in segments:
        span = _clip_interval(a, b, half_width, half_length, corner_radius)
        if span is None:
            continue
        lo, hi = span
        dx, dy = b[0] - a[0], b[1] - a[1]
        p_lo = (a[0] + lo * dx, a[1] + lo * dy)
        p_hi = (a[0] + hi * dx, a[1] + hi * dy)
        # A center endpoint followed by a bend hidden under the pad is also a defect.
        inside_tracks.append((p_lo, p_hi))
        if math.dist(p_lo, p_hi) > EPS and math.degrees(math.atan2(abs(dx), abs(dy))) > ANGLE_DEG:
            issues.append("a segment bends or runs diagonally inside the pad")
        # A segment ending on the edge is counted by its outward continuation.
        if _pad_sdf(b[0], b[1], half_width, half_length, corner_radius) > EPS:
            crossings.append(_classify_crossing(p_hi, (dx, dy), half_width, half_length, corner_radius))
        if _pad_sdf(a[0], a[1], half_width, half_length, corner_radius) > EPS:
            crossings.append(_classify_crossing(p_lo, (-dx, -dy), half_width, half_length, corner_radius))
    # Prefer the actual center endpoint over a second, disconnected stub that
    # merely starts within the placement tolerance.
    endpoints = {point for a, b, _ in segments for point in (a, b)}
    nearest = min((math.hypot(*p) for p in endpoints), default=math.inf)
    centers = {p for p in endpoints if math.hypot(*p) <= min(nearest, OFFSET_MM) + EPS}
    if not centers:
        issues.append("no trace endpoint at the pad center")
    elif len(crossings) == 1 and not reaches_crossing(inside_tracks, centers, crossings[0]["point_mm"]):
        issues.append("trace centerline is discontinuous from the pad center to the edge crossing")
    if len(crossings) != 1:
        issues.append(f"expected one edge crossing, found {len(crossings)}")
    for item in crossings:
        if not item["facing_edge"]:
            issues.append("trace crosses a different pad edge")
        if item["offset_mm"] > OFFSET_MM + EPS:
            issues.append(f'edge offset {item["offset_mm"]:.4f} mm exceeds {OFFSET_MM:.2f} mm')
        if item["angle_deg"] > ANGLE_DEG + EPS:
            issues.append(f'entry is {item["angle_deg"]:.1f} degrees from perpendicular')
    return {"crossings": crossings, "issues": list(dict.fromkeys(issues))}


def write_closeup(path, w, h, segments, crossings, corner_radius=0.0):
    """Pad-local, equal-scale PNG of measured copper with centerline and crossing markers."""
    size = 288
    scale = size / (2 * max(w, h) * 1.7)
    def pixel(x, y):
        return (round(size / 2 + x * scale), round(size / 2 - y * scale))
    rows = [bytearray(size) for _ in range(size)]
    def stroke(a, b, width, color):
        ax, ay = pixel(*a)
        bx, by = pixel(*b)
        dx, dy = bx - ax, by - ay
        length2 = dx * dx + dy * dy
        r = max(1, width * scale / 2)
        for y in range(max(0, math.floor(min(ay, by) - r)), min(size, math.ceil(max(ay, by) + r) + 1)):
            for x in range(max(0, math.floor(min(ax, bx) - r)), min(size, math.ceil(max(ax, bx) + r) + 1)):
                t = max(0, min(1, ((x-ax)*dx + (y-ay)*dy) / length2)) if length2 else 0
                if (x-ax-t*dx)**2 + (y-ay-t*dy)**2 <= r*r:
                    rows[y][x] = color
    left, top = pixel(-w, h)
    right, bottom = pixel(w, -h)
    if corner_radius <= EPS:
        for y in range(top, bottom + 1):
            rows[y][left:right+1] = bytes([1]) * (right-left+1)
    else:
        for py in range(max(0, top), min(size, bottom + 1)):
            ly = (size / 2 - py) / scale
            for px in range(max(0, left), min(size, right + 1)):
                lx = (px - size / 2) / scale
                if _pad_sdf(lx, ly, w, h, corner_radius) <= 0.5 / scale:
                    rows[py][px] = 1
    for a, b, width in segments:
        stroke(a, b, width, 2)
    for a, b, _ in segments:
        stroke(a, b, 1.5 / scale, 3)
    stroke((0, -h), (0, h * 1.45), 1.5 / scale, 4)
    iw = max(0.0, w - corner_radius)
    guide = iw if iw > EPS else w * 0.25
    stroke((-guide, h), (guide, h), 1.5 / scale, 4)
    for item in crossings:
        xy = item["point_mm"]
        stroke(xy, xy, 10 / scale, 5)
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    palette = bytes.fromhex("192331 943a44 e2785d f9e5ca 6ed4e5 ffd562")
    raw = b"".join(b"\0" + row for row in rows)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 3, 0, 0, 0))
                     + chunk(b"PLTE", palette) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _pad_corner_radius(name, pad, layer, sx, sy, pcbnew):
    if pad.GetAttribute() != pcbnew.PAD_ATTRIB_SMD:
        raise ValueError(f"{name}: centered exits require an SMD pad")
    shape = pad.GetShape(layer)
    if shape == pcbnew.PAD_SHAPE_RECTANGLE:
        return 0.0
    if shape == pcbnew.PAD_SHAPE_ROUNDRECT:
        ratio = pad.GetRoundRectRadiusRatio(layer)
        if not (0.0 <= ratio <= 0.5):
            raise ValueError(f"{name}: unsupported rounded-rectangle radius ratio {ratio}")
        return min(sx, sy) * ratio
    if shape == pcbnew.PAD_SHAPE_OVAL:
        return min(sx, sy) / 2.0
    raise ValueError(f"{name}: centered exits require a rectangular, rounded-rectangle or oval SMD pad")


def measure(board, pads, net, output):
    """Use KiCad's absolute pad copper centers, rotation, size and copper layer."""
    import pcbnew
    def mm(vec):
        return pcbnew.ToMM(vec.x), pcbnew.ToMM(vec.y)
    reports = []
    for i, (name, pad) in enumerate(pads.items()):
        layer = pad.GetLayer()
        cx, cy = mm(pad.ShapePos(layer))
        sx, sy = mm(pad.GetSize(layer))
        corner_radius = _pad_corner_radius(name, pad, layer, sx, sy, pcbnew)
        if abs(sx - sy) < EPS or min(sx, sy) <= 0:
            raise ValueError(f"{name}: pad has no unique short edge")
        angle_deg = pad.GetOrientationDegrees()
        def local(x, y):
            u, v = rg.world_to_local(cx, cy, angle_deg, x, y)
            return (u, v) if sy > sx else (-v, u)
        other = next(p for key, p in pads.items() if key != name)
        toward = local(*mm(other.ShapePos(other.GetLayer())))[1]
        if abs(toward) < EPS:
            raise ValueError(f"{name}: neither short edge faces the other pad")
        sign = 1 if toward > 0 else -1
        def position(vec):
            x, y = local(*mm(vec))
            return sign*x, sign*y
        w, h = sorted((sx/2, sy/2))
        radius = max(w, h) * 1.75
        segments = []
        extra_issues = []
        for track in board.GetTracks():
            if track.GetNetname() != net or not track.IsOnLayer(layer):
                continue
            if type(track) is not pcbnew.PCB_TRACK:
                if track.GetBoundingBox().Intersects(pad.GetBoundingBox()):
                    extra_issues.append("via or curved track touches the pad; move it clear of the pad edge")
                continue
            a, b = position(track.GetStart()), position(track.GetEnd())
            width = pcbnew.ToMM(track.GetWidth())
            margin = radius + width/2
            if any(min(a[k], b[k]) > margin or max(a[k], b[k]) < -margin for k in (0, 1)):
                continue
            segments.append((a, b, width))
        report = crossing_report(w, h, segments, corner_radius)
        if extra_issues:
            report["issues"] = list(dict.fromkeys([*report["issues"], *extra_issues]))
        picture = output.with_name(f"{output.stem}-pad{i+1}.png")
        write_closeup(picture, w, h, segments, report["crossings"], corner_radius)
        reports.append(dict(report, pad=name, picture=picture.name))
    return reports


def _board_pads(board_text: str) -> dict[str, list[dict]]:
    """Index every pad in a saved .kicad_pcb by 'REF.PAD' using KiCad's board coordinates."""
    from kicad_sexp import child, children, parse, prop
    root = parse(board_text)
    if not isinstance(root, list) or not root or root[0] != "kicad_pcb":
        raise ValueError("not a KiCad board")
    net_map = rg.board_net_map(root)
    by_name: dict[str, list[dict]] = {}
    for fp in children(root, "footprint"):
        ref = prop(fp, "Reference")
        if not ref:
            for txt in children(fp, "fp_text"):
                if len(txt) >= 3 and txt[1] == "reference":
                    ref = txt[2]
                    break
        if not ref:
            continue
        fp_at = child(fp, "at") or ["at", "0", "0", "0"]
        fx, fy = float(fp_at[1]), float(fp_at[2])
        frot = float(fp_at[3]) if len(fp_at) > 3 else 0.0
        for pad in children(fp, "pad"):
            if len(pad) < 4 or not pad[1]:
                continue
            pad_at = child(pad, "at") or ["at", "0", "0"]
            px, py = float(pad_at[1]), float(pad_at[2])
            prot = float(pad_at[3]) if len(pad_at) > 3 else 0.0
            bx, by = rg.local_to_world(fx, fy, frot, px, py)
            off_node = child(pad, "offset")
            if off_node and len(off_node) >= 3:
                try:
                    ox, oy = float(off_node[1]), float(off_node[2])
                    bx, by = rg.local_to_world(bx, by, prot, ox, oy)
                except (TypeError, ValueError):
                    pass
            size = child(pad, "size")
            sx, sy = (float(size[1]), float(size[2])) if size and len(size) >= 3 else (0.0, 0.0)
            net_name = rg.resolve_item_net(pad, net_map)
            rr = child(pad, "roundrect_rratio")
            ratio = float(rr[1]) if rr and len(rr) > 1 else (0.25 if pad[3] == "roundrect" else 0.0)
            name = f"{ref}.{pad[1]}"
            by_name.setdefault(name, []).append({
                "name": name, "kind": pad[2], "shape": pad[3], "center": (bx, by),
                "size": (sx, sy), "angle_deg": prot, "net": net_name, "round_ratio": ratio,
            })
    return by_name


def preflight_tasks(board_text: str, tasks: list[tuple[str, str]]) -> list[str]:
    """Check every routing task against the saved .kicad_pcb before starting KiCad or an attempt.

    Returns one human-readable line per static problem found across all tasks, or [] when clean.
    """
    from kicad_sexp import child, children, copper_layers, parse
    from routing_evidence import parse_task_connections, parse_task_mode, parse_task_pad_exits, parse_task_pours

    root = parse(board_text)
    if not isinstance(root, list) or not root or root[0] != "kicad_pcb":
        raise ValueError("not a KiCad board")
    layers_list = copper_layers(root)
    valid_layers = set(layers_list) if child(root, "layers") is not None else {"F.Cu", "B.Cu"}
    pads = _board_pads(board_text)
    defined_nets = {str(n[2]) for n in children(root, "net") if len(n) >= 3 and str(n[2])}
    defined_nets |= {p["net"] for plist in pads.values() for p in plist if p["net"]}
    problems: list[str] = []
    for task_name, task_text in tasks:
        try:
            targets = parse_task_connections(task_text)
            centered = parse_task_pad_exits(task_text, targets)
            pours = parse_task_pours(task_text)
            parse_task_mode(task_text)
        except ValueError as error:
            problems.append(f"{task_name}: {error}")
            continue
        for pour in pours:
            for layer in pour["layers"]:
                if layer not in valid_layers:
                    problems.append(f"{task_name}: copper layer {layer!r} is not enabled on this board")
            if pour["net"] not in defined_nets:
                problems.append(f"{task_name}: net {pour['net']!r} is not on this board")
        if not targets:
            continue
        for start, end, net in targets:
            resolved = {}
            for pad_name in (start, end):
                matches = pads.get(pad_name, [])
                if not matches:
                    problems.append(f"{task_name}: {pad_name} is not on the board")
                elif len(matches) > 1:
                    wrong = [p for p in matches if p["net"] != net]
                    if wrong:
                        problems.append(
                            f"{task_name}: {pad_name} has pad(s) on {wrong[0]['net']!r}, expected {net!r}"
                        )
                    elif centered:
                        problems.append(
                            f"{task_name}: {pad_name} occurs more than once on this board "
                            "(centered exits require a single pad)"
                        )
                    else:
                        resolved[pad_name] = matches[0]
                else:
                    pad = matches[0]
                    if pad["net"] != net:
                        problems.append(f"{task_name}: {pad_name} is on {pad['net']!r}, expected {net!r}")
                    resolved[pad_name] = pad
            if not centered or len(resolved) != 2:
                continue
            for pad_name in (start, end):
                pad = resolved[pad_name]
                other = resolved[end if pad_name == start else start]
                if pad["kind"] != "smd":
                    problems.append(f"{task_name}: {pad_name}: centered exits require an SMD pad (found {pad['kind']})")
                    continue
                if pad["shape"] not in SUPPORTED_SHAPES:
                    problems.append(f"{task_name}: {pad_name}: centered exits require a rectangular, rounded-rectangle "
                                    f"or oval SMD pad (found {pad['shape']})")
                    continue
                if pad["shape"] == "roundrect" and not (0.0 <= pad["round_ratio"] <= 0.5):
                    problems.append(f"{task_name}: {pad_name}: unsupported rounded-rectangle radius ratio {pad['round_ratio']}")
                    continue
                sx, sy = pad["size"]
                if abs(sx - sy) < EPS or min(sx, sy) <= 0:
                    problems.append(f"{task_name}: {pad_name}: pad has no unique short edge")
                    continue
                cx, cy = pad["center"]
                ox, oy = other["center"]
                angle = math.radians(pad["angle_deg"])
                dx, dy = ox - cx, oy - cy
                u = dx * math.cos(angle) - dy * math.sin(angle)
                v = dx * math.sin(angle) + dy * math.cos(angle)
                toward = v if sy > sx else u
                if abs(toward) < EPS:
                    problems.append(f"{task_name}: {pad_name}: neither short edge faces the other pad")
    return problems
