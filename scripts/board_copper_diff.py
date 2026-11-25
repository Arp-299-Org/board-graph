#!/usr/bin/env python3
"""What a routing batch changed on the board, by net, against a base board.

    py -3 scripts/board_copper_diff.py --base origin/main --nets BTN_1,BTN_2 \
        [--board <file>.kicad_pcb] [--fix "TFT_SCK=J2 pin map corrected"] [--json out.json]

The board, and its path inside a revision given as --base, is the one its description names
(scripts/board_description.py: BOARD_GRAPH_BOARD, else board/board.json).

Compares tracks, arcs, vias and zone outlines (not fills, which every refill
redraws) by net, and every part's position and side. A batch answers for each
change: copper added on a net in --nets is the batch's own work; earlier copper
removed or moved, on any net, and any moved part, is a repair that --fix must name
(the net or part, with its reason). A track only split where a new branch starts,
its pieces covering exactly the old track, is not a change. Anything else is
listed as undeclared and the exit status is 1, so earlier copper never moves
without being named. Silk, board edges and other graphics are not compared.
Text parse only: no KiCad, no writes besides --json.
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib
import subprocess
import sys

import tool_paths

if __name__ == "__main__":
    tool_paths.take_argument()

import board_description  # noqa: E402
import kicad_rules_geom as rg  # noqa: E402
from kicad_sexp import child, children, normalize_angles, parse, prop  # noqa: E402

ROOT = tool_paths.project_root()
BOARD = board_description.load().rel("pcb")


def read_board(spec):
    """A board file path, or a git revision whose committed board is read."""
    path = pathlib.Path(spec)
    if path.is_file():
        return path.read_text(encoding="utf-8")
    shown = subprocess.run(["git", "show", f"{spec}:{BOARD}"], cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8")
    if shown.returncode:
        raise ValueError(f"{spec} is neither a board file nor a revision with {BOARD}: {shown.stderr.strip()}")
    return shown.stdout


def net_of(node, net_map: dict[str, str] | None = None):
    return rg.resolve_item_net(node, net_map or {}).lstrip("/")


def point(node, tag):
    found = child(node, tag)
    return (round(float(found[1]), 4), round(float(found[2]), 4))


def items(text):
    """{(kind, net or reference): set of item keys} for everything compared."""
    board = parse(text)
    net_map = rg.board_net_map(board)
    found = collections.defaultdict(set)
    for kind in ("segment", "arc"):
        for t in children(board, kind):
            ends = tuple(sorted([point(t, "start"), point(t, "end")]))
            mid = point(t, "mid") if kind == "arc" else None
            found[("net", net_of(t, net_map))].add((kind, child(t, "layer")[1], float(child(t, "width")[1]), ends, mid))
    for v in children(board, "via"):
        found[("net", net_of(v, net_map))].add(("via", point(v, "at"), float(child(v, "size")[1]),
                                                float(child(v, "drill")[1]), tuple(child(v, "layers")[1:])))
    for z in children(board, "zone"):
        layers = tuple((child(z, "layers") or child(z, "layer"))[1:])
        outline = tuple(tuple((round(float(xy[1]), 4), round(float(xy[2]), 4)) for xy in children(child(p, "pts"), "xy"))
                        for p in children(z, "polygon"))
        name = child(z, "name")
        name = name[1] if name else ""
        # A rule area has no net: it answers by its name, so --fix can name a keep-out a batch reshapes.
        znet = "" if child(z, "keepout") else net_of(z, net_map)
        key = ("net", znet) if znet else ("area", name or "(unnamed)")
        found[key].add(("zone", name, layers, outline))
    for fp in children(board, "footprint"):
        at = normalize_angles(child(fp, "at"))
        found[("part", prop(fp, "Reference"))].add(("position", child(fp, "layer")[1],
                                                    tuple(round(float(v), 4) if isinstance(v, str) and v != "unlocked" else v for v in at[1:])))
    return found


def is_split(old, added):
    """True when added straight tracks of the same layer and width lie along `old` and cover it end to end."""
    if old[0] != "segment":
        return False
    (ax, ay), (bx, by) = old[3]
    length = ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5
    if not length:
        return False
    spans = []
    for item in added:
        if item[0] != "segment" or item[1:3] != old[1:3]:
            continue
        ts = []
        for px, py in item[3]:
            if abs((bx - ax) * (py - ay) - (by - ay) * (px - ax)) / length > 1e-3:
                break
            ts.append(((px - ax) * (bx - ax) + (py - ay) * (by - ay)) / length ** 2)
        else:
            if min(ts) > -1e-4 and max(ts) < 1 + 1e-4:
                spans.append(sorted(ts))
    reach = 0.0
    for start, end in sorted(spans):
        if start > reach + 1e-4:
            return False
        reach = max(reach, end)
    return reach >= 1 - 1e-4


def where(item):
    if item[0] == "via":
        return f"via at {item[1]}"
    if item[0] in ("segment", "arc"):
        return f"{item[0]} {item[1]} {item[3][0]} to {item[3][1]}"
    if item[0] == "position":
        return f"{item[1]} at {item[2]}"
    return f"{item[0]} {item[1]} on {', '.join(item[2])}"


def compare(base_text, board_text, nets, fixes):
    base, now = items(base_text), items(board_text)
    report = {"batch": [], "fixes": [], "undeclared": []}
    for key in sorted(set(base) | set(now)):
        added, removed = now.get(key, set()) - base.get(key, set()), base.get(key, set()) - now.get(key, set())
        if not added and not removed:
            continue
        kind, name = key
        count = lambda found: collections.Counter(i[0] for i in found)
        if kind == "net" and name in nets:
            splits = {i for i in removed if is_split(i, added)}
            changed = removed - splits
            report["batch"].append({"kind": kind, "name": name, "added": count(added), "removed": {},
                                    "split": count(splits)})
            if not changed:
                continue
            row = {"kind": kind, "name": name, "added": {}, "removed": count(changed),
                   "earlier": sorted(where(i) for i in changed)}
        else:
            row = {"kind": kind, "name": name or "(no net)", "added": count(added), "removed": count(removed),
                   "earlier": sorted(where(i) for i in removed)}
        if name in fixes:
            report["fixes"].append({**row, "reason": fixes[name]})
        else:
            report["undeclared"].append(row)
    for name in sorted(set(fixes) - {r["name"] for r in report["fixes"]}):
        report["undeclared"].append({"kind": "fix", "name": name, "added": {}, "removed": {},
                                     "note": "declared as a fix but nothing changed"})
    return report


def describe(row):
    changes = ([f"+{n} {k}" for k, n in sorted(row["added"].items())] + [f"-{n} {k}" for k, n in sorted(row["removed"].items())]
               + [f"{n} {k} split" for k, n in sorted(row.get("split", {}).items())])
    line = f'{row["kind"]} {row["name"]}: {", ".join(changes) or row.get("note", "")}' + (
        f' ({row["reason"]})' if row.get("reason") else "")
    return "\n    ".join([line, *(f"was: {e}" for e in row.get("earlier", []))])


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", required=True, help="a board file or a git revision (e.g. origin/main)")
    parser.add_argument("--board", default=str(ROOT / BOARD))
    parser.add_argument("--nets", required=True, help="comma-separated nets the batch routes")
    parser.add_argument("--fix", action="append", default=[], metavar="NAME=REASON",
                        help="a net (a batch net too) or part reference whose earlier copper or position this batch "
                             "repairs, and why")
    parser.add_argument("--json", type=pathlib.Path)
    args = parser.parse_args()
    try:
        nets = {n.strip().lstrip("/") for n in args.nets.split(",") if n.strip()}
        fixes = {}
        for item in args.fix:
            name, _, reason = item.partition("=")
            if not name.strip() or not reason.strip():
                raise ValueError(f"--fix needs NAME=REASON: {item}")
            fixes[name.strip().lstrip("/")] = reason.strip()
        report = compare(read_board(args.base), read_board(args.board), nets, fixes)
    except (OSError, ValueError) as error:
        parser.exit(2, f"board_copper_diff: {error}\n")
    for label, key in (("Batch nets", "batch"), ("Declared fixes", "fixes"), ("Undeclared changes", "undeclared")):
        print(f"{label}: {len(report[key]) or 'none'}")
        for row in report[key]:
            print("  " + describe(row))
    if args.json:
        args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    sys.exit(1 if report["undeclared"] else 0)


if __name__ == "__main__":
    main()
