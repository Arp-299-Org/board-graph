#!/usr/bin/env python3
"""Route elegance audit: is each connection as simple as it could be?

`kicad_route_quality.py` proves geometry is *legal*: H, V, or 45 only, no hard
corners, no micro-segments. Legal is not the same as beautiful. A run can be
perfectly legal and still wander: an extra dogleg where one 45 would do, a hump
that adds vertices and length, or a power rail that necks down mid-run for no
reason. Those read as sloppy to anyone who looks at the board, and no existing
gate catches them.

This audit measures two things per connection:

  detour      actual copper length against the shortest legal 45-capable path
              between the same two endpoints
  vertices    actual corner count against the minimum that path needs

plus width consistency per net, so a rail that thins down is reported with the
exact location.

A track arc is listed under track_arcs and left out of the chains: the audit
measures 45-degree runs and cannot say whether an arc is a detour.

Findings are advisory by design. A genuine obstacle (a pad sitting on the
trace's line, a keepout, a dense pad field) forces a real detour, and this tool
cannot tell that from laziness. It produces the review list; a human decides.
Use --max-detour / --max-excess-vertices to set the bar for a build gate.
"""
from __future__ import annotations

import argparse
import json
import math
import pathlib
from collections import defaultdict

import pcbnew

SQRT2 = math.sqrt(2.0)


def mm(value) -> float:
    return pcbnew.ToMM(value)


def point(vector) -> tuple[float, float]:
    return (round(mm(vector.x), 6), round(mm(vector.y), 6))


def ideal_path(a: tuple[float, float], b: tuple[float, float]) -> tuple[float, int]:
    """Shortest length and corner count for an H/V/45 path from a to b."""
    dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
    diagonal, straight = min(dx, dy), abs(dx - dy)
    length = diagonal * SQRT2 + straight
    # one segment if pure H, V, or exact 45; otherwise a diagonal plus a run
    corners = 0 if diagonal < 1e-9 or straight < 1e-9 else 1
    return length, corners


def load_chains(board_path: pathlib.Path):
    board = pcbnew.LoadBoard(str(board_path))
    segments, arcs = [], []
    for item in board.GetTracks():
        if isinstance(item, pcbnew.PCB_ARC):
            arcs.append({
                "net": item.GetNetname(),
                "layer": str(board.GetLayerName(item.GetLayer())),
                "from_mm": list(point(item.GetStart())),
                "to_mm": list(point(item.GetEnd())),
                "width_mm": round(mm(item.GetWidth()), 6),
            })
            continue
        if type(item) is not pcbnew.PCB_TRACK:
            continue
        segments.append({
            "net": item.GetNetname(),
            "layer": int(item.GetLayer()),
            "layer_name": str(board.GetLayerName(item.GetLayer())),
            "a": point(item.GetStart()),
            "b": point(item.GetEnd()),
            "w": round(mm(item.GetWidth()), 6),
        })
    anchors = defaultdict(set)
    for footprint in board.GetFootprints():
        for pad in footprint.Pads():
            anchors[pad.GetNetname()].add(point(pad.GetPosition()))
    for item in board.GetTracks():
        if isinstance(item, pcbnew.PCB_VIA):
            anchors[item.GetNetname()].add(point(item.GetPosition()))

    incident = defaultdict(list)
    for index, s in enumerate(segments):
        incident[(s["net"], s["layer"], s["a"])].append(index)
        incident[(s["net"], s["layer"], s["b"])].append(index)

    def is_anchor(net, layer, p):
        # Deliberately NOT splitting on width change: a width transition inside
        # one run is one of the things being audited.
        return p in anchors[net] or len(incident[(net, layer, p)]) != 2

    visited, chains = set(), []
    for index, s in enumerate(segments):
        if index in visited:
            continue
        net, layer = s["net"], s["layer"]
        points = [s["a"], s["b"]]
        members = [index]
        visited.add(index)
        for end in (0, 1):
            while True:
                tip = points[0] if end == 0 else points[-1]
                if is_anchor(net, layer, tip):
                    break
                nxt = [i for i in incident[(net, layer, tip)] if i not in visited]
                if len(nxt) != 1:
                    break
                other = segments[nxt[0]]
                far = other["b"] if other["a"] == tip else other["a"]
                if far in points:
                    break
                visited.add(nxt[0])
                members.append(nxt[0])
                points.insert(0, far) if end == 0 else points.append(far)
        chains.append({
            "net": net,
            "layer_name": segments[members[0]]["layer_name"],
            "points": points,
            "widths": sorted({segments[i]["w"] for i in members}),
        })
    return chains, segments, arcs


def audit(board_path: pathlib.Path, max_detour: float, max_excess: int):
    chains, segments, arcs = load_chains(board_path)
    detours, width_findings = [], []

    for chain in chains:
        points = chain["points"]
        if len(points) < 2:
            continue
        start, end = points[0], points[-1]
        actual = sum(math.dist(x, y) for x, y in zip(points, points[1:]))
        ideal, ideal_corners = ideal_path(start, end)
        corners = len(points) - 2
        if ideal < 0.5:              # sub-millimetre stubs prove nothing
            continue
        ratio = actual / ideal if ideal else 1.0
        excess = corners - ideal_corners
        if ratio > max_detour or excess > max_excess:
            detours.append({
                "net": chain["net"],
                "layer": chain["layer_name"],
                "from_mm": list(start),
                "to_mm": list(end),
                "length_mm": round(actual, 3),
                "shortest_legal_mm": round(ideal, 3),
                "detour_ratio": round(ratio, 3),
                "corners": corners,
                "shortest_legal_corners": ideal_corners,
                "excess_corners": excess,
            })
        if len(chain["widths"]) > 1:
            width_findings.append({
                "net": chain["net"],
                "layer": chain["layer_name"],
                "from_mm": list(start),
                "to_mm": list(end),
                "widths_mm": chain["widths"],
                "issue": "width changes inside a single run",
            })

    by_net = defaultdict(set)
    for s in segments:
        by_net[s["net"]].add(s["w"])
    necked = []
    for net, widths in sorted(by_net.items()):
        if len(widths) > 1 and max(widths) / min(widths) > 1.2:
            narrowest = min(widths)
            where = [s for s in segments if s["net"] == net and s["w"] == narrowest]
            necked.append({
                "net": net,
                "widths_mm": sorted(widths),
                "ratio": round(max(widths) / min(widths), 3),
                "narrowest_mm": narrowest,
                "narrowest_segments": len(where),
                "first_narrow_at_mm": list(where[0]["a"]) if where else None,
            })

    detours.sort(key=lambda f: -f["detour_ratio"])
    return {
        "schema": "board-graph-route-elegance/v1",
        "board": str(board_path),
        "thresholds": {"max_detour_ratio": max_detour, "max_excess_corners": max_excess},
        "summary": {
            "chains": len(chains),
            "detour_findings": len(detours),
            "mixed_width_runs": len(width_findings),
            "necked_nets": len(necked),
            "track_arcs": len(arcs),
        },
        "detours": detours,
        "mixed_width_runs": width_findings,
        "necked_nets": necked,
        "track_arcs": arcs,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--board", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path)
    parser.add_argument("--max-detour", type=float, default=1.08,
                        help="flag a run longer than this multiple of the shortest legal path")
    parser.add_argument("--max-excess-corners", type=int, default=1,
                        help="flag a run with more than this many corners beyond the minimum")
    parser.add_argument("--fail", action="store_true",
                        help="exit non-zero when anything is flagged (for use as a gate)")
    parser.add_argument("--top", type=int, default=25)
    args = parser.parse_args()

    report = audit(args.board.resolve(), args.max_detour, args.max_excess_corners)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"], sort_keys=True))
    for finding in report["detours"][:args.top]:
        print(f"  detour x{finding['detour_ratio']:.2f} {finding['net']:16} {finding['layer'][:9]:9} "
              f"{tuple(finding['from_mm'])} -> {tuple(finding['to_mm'])}  "
              f"{finding['length_mm']:.2f} mm vs {finding['shortest_legal_mm']:.2f} mm, "
              f"corners {finding['corners']} vs {finding['shortest_legal_corners']}")
    for finding in report["mixed_width_runs"][:args.top]:
        print(f"  width   {finding['net']:16} {finding['widths_mm']} in one run "
              f"{tuple(finding['from_mm'])} -> {tuple(finding['to_mm'])}")
    for finding in report["necked_nets"][:args.top]:
        print(f"  neck    {finding['net']:16} widths {finding['widths_mm']} "
              f"(x{finding['ratio']}), {finding['narrowest_segments']} narrow segment(s), "
              f"first at {tuple(finding['first_narrow_at_mm'])}")
    for finding in report["track_arcs"][:args.top]:
        print(f"  arc     {finding['net']:16} {finding['layer'][:9]:9} "
              f"{tuple(finding['from_mm'])} -> {tuple(finding['to_mm'])}  not judged")
    if args.fail and any(report["summary"][key] for key in
                         ("detour_findings", "mixed_width_runs", "necked_nets", "track_arcs")):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
