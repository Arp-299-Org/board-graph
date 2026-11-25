#!/usr/bin/env python3
"""Whether every hand-soldered joint on the board can be made: does the real pin fit the finished hole, is the land
open to the iron on both faces, and is it kept off the planes by thermal spokes.

Read from the saved board's text and the parts data its description names (scripts/board_description.py), no KiCad. For every plated through-hole pad,
grouped by part, one row per distinct joint kind:

- the hole as drawn (round, or a slot) and its smallest finished size at the fabricator's tolerance (TOLERANCE);
- the pad's size and its least annular ring, at the largest finished hole;
- whether its layers open the mask on both faces;
- the zone connection the settings ask for: the pad's override, else its footprint's, else each plane's on the
  pad's net; custom DRC rules can also set one, and the script says when the rules file has any;
- the real pin from parts.json (`pins`, taken from the datasheet) and the fit: the pin's largest cross dimension
  (the diagonal of a flat pin in a round hole; both axes in a slot, the pin's long side along the slot) against the
  smallest finished hole, as a margin in mm. FITS, DOES NOT FIT (margin at or below zero), or NOT PROVEN (no pin
  size on record). No minimum margin is invented: the number is printed, with the maker's recommended hole beside
  it when its datasheet gives one.

  py -3 scripts/board_manual_joints.py [--board <pcb>] [--parts <json>] [--write <md>] [--json <json>]
      [--board-description <path>]

Exit 1 on any DOES NOT FIT, any pad without mask on a face, or any solid plane setting. NOT PROVEN is printed and
counted but does not change the exit status; the Footprints card treats it as a finding.
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import tool_paths  # noqa: E402

if __name__ == "__main__":
    tool_paths.take_argument()

from kicad_sexp import child, children, parse, prop  # noqa: E402
import board_description  # noqa: E402

ROOT = tool_paths.project_root()
DESCRIPTION = board_description.load()
BOARD = DESCRIPTION.path("pcb")
PARTS = DESCRIPTION.path("parts_data")

# The finished size of a plated hole against its drawn size. Applied to plated slots as to round holes.
TOLERANCE = {"plus_mm": 0.13, "minus_mm": 0.08,
             "source": "JLCPCB capabilities page, plated hole finished size +0.13 / -0.08 mm, read 2026-11-11"}

GRAPH = ROOT / "tools" / "board_graph.json"

FITS, DOES_NOT_FIT, NOT_PROVEN = "FITS", "DOES NOT FIT", "NOT PROVEN"
AS_MAKER, NOT_FITTED = "AS THE MAKER DRAWS IT", "NOT FITTED"

# KiCad's zone connection codes, as a pad or footprint writes (zone_connect n).
ZONE_CONNECT = {"0": "none", "1": "thermal", "2": "solid", "3": "thermal (through-hole only)"}
# A zone's (connect_pads <mode> ...): no mode is thermal.
ZONE_MODES = {"yes": "solid", "no": "none", "thru_hole_only": "thermal (through-hole only)"}

# Each hand-soldered part by what it is, as the board's description names it (manual_joints.names); a part it does
# not name is called by its value.
NAMES = DESCRIPTION.manual_joints.get("names", {})
# What the report says about a recommended pin-to-hole clearance; the description's manual_joints.clearance_note
# replaces it.
CLEARANCE_NOTE = ("The repository records no recommended pin-to-hole clearance. The maker's recommended hole is "
                  "printed where its datasheet gives one")


def mm(value) -> float:
    return float(value)


def fmt(value: float | None) -> str:
    return "?" if value is None else f"{value:.2f}"


def part_name(ref: str, value: str | None, names: dict | None = None) -> str:
    return f"{(NAMES if names is None else names).get(ref, value or 'the part')} ({ref})"


def hole_of(pad) -> dict:
    """The drilled hole in the pad's own axes: (x, y) sizes, offset, and whether it is a slot."""
    drill = child(pad, "drill")
    atoms = [a for a in drill[1:] if not isinstance(a, list)]
    offset = child(drill, "offset")
    off = (abs(mm(offset[1])), abs(mm(offset[2]))) if offset else (0.0, 0.0)
    if atoms and atoms[0] == "oval":
        sizes = [mm(a) for a in atoms[1:]]
        x, y = sizes[0], sizes[1] if len(sizes) > 1 else sizes[0]
    else:
        x = y = mm(atoms[0])
    return {"x": x, "y": y, "offset": off, "slot": x != y}


def hole_text(hole: dict, grow: float = 0.0) -> str:
    small, large = sorted((hole["x"] + grow, hole["y"] + grow))
    return f"round {small:.2f}" if not hole["slot"] else f"slot {small:.2f} x {large:.2f}"


def least_ring(pad, hole: dict) -> float:
    """The narrowest copper between the largest finished hole and the pad's edge, along the pad's axes."""
    size = child(pad, "size")
    sx, sy = mm(size[1]), mm(size[2])
    grow = TOLERANCE["plus_mm"]
    return min((sx - hole["x"] - grow) / 2 - hole["offset"][0], (sy - hole["y"] - grow) / 2 - hole["offset"][1])


def mask_faces(pad) -> list[str]:
    layers = set(child(pad, "layers")[1:])
    return [face for face in ("F", "B") if "*.Mask" in layers or f"{face}.Mask" in layers]


def copper_layers(pad) -> set[str] | None:
    """The pad's named copper layers, or None for every copper layer (*.Cu)."""
    layers = child(pad, "layers")[1:]
    return None if "*.Cu" in layers else {layer for layer in layers if layer.endswith(".Cu")}


def net_of(node) -> str | None:
    net = child(node, "net")
    return str(net[-1]) if net and len(net) > 1 else None


def planes(board) -> list[dict]:
    """Every copper zone that is not a rule area: its net, layers and pad connection mode."""
    found = []
    for zone in children(board, "zone"):
        if child(zone, "keepout") or not net_of(zone):
            continue
        layers = child(zone, "layers") or child(zone, "layer")
        connect = child(zone, "connect_pads")
        mode = connect[1] if connect and len(connect) > 1 and not isinstance(connect[1], list) else None
        found.append({"net": net_of(zone), "layers": set(layers[1:]), "mode": ZONE_MODES.get(mode, "thermal"),
                      "name": str((child(zone, "name") or [None, None])[1] or (child(zone, "uuid") or ["", "?"])[1])})
    return found


def connection(pad, fp, zones: list[dict]) -> tuple[str, bool]:
    """The connection the settings ask for, in words, and whether it is solid onto a plane of the pad's net."""
    net, copper = net_of(pad), copper_layers(pad)
    mine = [z for z in zones if z["net"] == net and (copper is None or z["layers"] & copper)]
    for node, whose in ((pad, "pad"), (fp, "footprint")):
        setting = child(node, "zone_connect")
        if setting:
            kind = ZONE_CONNECT.get(str(setting[1]), str(setting[1]))
            if not mine:
                return f"{kind} ({whose} setting, no plane on its net)", False
            return f"{kind} ({whose} setting)", kind == "solid"
    if not mine:
        return "no plane on its net", False
    kinds = sorted({z["mode"] for z in mine})
    return f"{', '.join(kinds)} (plane setting)", "solid" in kinds


def pin_for(pins: list[dict], number: str) -> dict | None:
    for pin in pins:
        if "pads" not in pin or number in pin["pads"]:
            return pin
    return None


def pin_size(pin: dict, key: str) -> float | None:
    value = pin.get(f"{key}_mm")
    return None if value is None else value + (pin.get(f"{key}_tol_mm") or 0.0)


def pin_text(pin: dict | None) -> str:
    if not pin or not pin.get("shape"):
        return "not on record"
    def one(key):
        nominal, tol = pin.get(f"{key}_mm"), pin.get(f"{key}_tol_mm")
        return "?" if nominal is None else f"{nominal:.2f}" + (f"+{tol:.2f}" if tol else "")
    if pin["shape"] == "round":
        return f"round {one('width')}"
    return f"{one('width')} x {one('thickness')}"


def fit(hole: dict, pin: dict | None) -> dict:
    """The margin between the pin and the smallest finished hole, and the verdict."""
    small, large = sorted((hole["x"] - TOLERANCE["minus_mm"], hole["y"] - TOLERANCE["minus_mm"]))
    if not pin or not pin.get("shape"):
        return {"verdict": NOT_PROVEN, "margin_mm": None, "how": "no pin size on record"}
    width = pin_size(pin, "width")
    thick = width if pin["shape"] == "round" else pin_size(pin, "thickness")
    if width is None or thick is None:
        known = width if width is not None else thick
        if known >= large:
            return {"verdict": DOES_NOT_FIT, "margin_mm": round(large - known, 3),
                    "how": f"the one cross dimension on record ({known:.2f}) is not less than the finished hole's "
                           f"largest side ({large:.2f})"}
        return {"verdict": NOT_PROVEN, "margin_mm": None,
                "how": f"only one cross dimension on record ({known:.2f}), which the finished hole "
                       f"{small:.2f} x {large:.2f} clears; the other is not on record"}
    a, b = max(width, thick), min(width, thick)
    if not hole["slot"]:
        need = a if pin["shape"] == "round" else math.hypot(a, b)
        margin = small - need
        how = (f"pin {need:.3f}" + ("" if pin["shape"] == "round" else " corner to corner")
               + f" in a finished hole of {small:.2f}")
    else:
        across = small - b
        if pin["shape"] == "round" or across <= 0:
            margin, how = across, f"pin {b:.2f} across a finished slot {small:.2f} wide"
        else:
            # The pin's corners must clear the slot's round ends: the length of slot the pin needs.
            r = small / 2
            end = max(0.0, a / 2 - math.sqrt(r * r - (b / 2) ** 2))
            along = large - (2 * end + small)
            margin = min(across, along)
            how = (f"across: {small:.2f} - {b:.2f} = {across:.3f}; along: the pin needs {2 * end + small:.3f} "
                   f"of the finished {large:.2f}, {along:.3f} spare")
    return {"verdict": FITS if margin > 0 else DOES_NOT_FIT, "margin_mm": round(margin, 3), "how": how}


def recommended_text(rec: dict | None) -> str:
    if not rec:
        return "not given"
    tol = f" +-{rec['tol_mm']:.2f}" if rec.get("tol_mm") else ""
    if rec["shape"] == "round":
        return f"round {rec['x_mm']:.2f}{tol}"
    return f"{rec['shape']} {rec['x_mm']:.2f} x {rec['y_mm']:.2f}{tol}"


def against_recommended(hole: dict, rec: dict | None) -> dict | None:
    """The board's hole as drawn, and its smallest finished size, against the maker's hole, side for side."""
    if not rec:
        return None
    wanted = sorted((rec["x_mm"], rec.get("y_mm") or rec["x_mm"]))
    drawn = sorted((hole["x"], hole["y"]))
    finished = [size - TOLERANCE["minus_mm"] for size in drawn]
    return {"drawn_short_mm": round(drawn[0] - wanted[0], 3), "drawn_long_mm": round(drawn[1] - wanted[1], 3),
            "finished_short_mm": round(finished[0] - wanted[0], 3),
            "finished_long_mm": round(finished[1] - wanted[1], 3),
            "drawn_ok": drawn[0] >= wanted[0] - 1e-9 and drawn[1] >= wanted[1] - 1e-9}


def compare_text(cmp: dict | None) -> str:
    if not cmp:
        return "no maker's hole"
    return (f"drawn {cmp['drawn_short_mm']:+.2f} / {cmp['drawn_long_mm']:+.2f}, "
            f"finished {cmp['finished_short_mm']:+.2f} / {cmp['finished_long_mm']:+.2f}")


def length_text(pin: dict | None, board_mm: float | None) -> str:
    """How far the pin reaches against the board's thickness, from the face the datasheet measures it from."""
    if not pin or pin.get("length_mm") is None:
        return "not given"
    length = pin["length_mm"]
    tol = f" +-{pin['length_tol_mm']:.2f}" if pin.get("length_tol_mm") else ""
    if board_mm is None:
        return f"{length:.2f}{tol}; board thickness not in the file"
    spare = length - board_mm
    where = (f"stands {spare:.2f} past the back face" if spare > 0 else
             f"ends {-spare:.2f} inside the hole, short of the back face")
    return f"{length:.2f}{tol} against a {board_mm:.2f} board: {where}"


def verdict(fitted: bool, pin_fit: dict, cmp: dict | None) -> str:
    """FITS on a whole pin that passes; DOES NOT FIT on a pin that fails or a hole drawn under the maker's;
    AS THE MAKER DRAWS IT on a hole drawn at least the maker's; else NOT PROVEN. A part not fitted is NOT FITTED."""
    if not fitted:
        return NOT_FITTED
    if pin_fit["verdict"] in (FITS, DOES_NOT_FIT):
        return pin_fit["verdict"]
    if cmp:
        return AS_MAKER if cmp["drawn_ok"] else DOES_NOT_FIT
    return NOT_PROVEN


def open_issues(graph_path: pathlib.Path | None) -> list[dict]:
    try:
        return json.loads(graph_path.read_text(encoding="utf-8-sig")).get("open_issues", []) if graph_path else []
    except (OSError, ValueError):
        return []


def issues_naming(ref: str, issues: list[dict]) -> list[str]:
    return [issue["id"] for issue in issues if f"({ref})" in str(issue.get("issue", ""))]


def board_thickness(board) -> float | None:
    general = child(board, "general")
    thickness = child(general, "thickness") if general else None
    return mm(thickness[1]) if thickness else None


def dru_note(board_path: pathlib.Path) -> str | None:
    rules = board_path.with_suffix(".kicad_dru")
    if rules.is_file() and "zone_connection" in rules.read_text(encoding="utf-8", errors="replace"):
        return f"{rules.name} has a zone_connection rule, which this text check does not apply"
    return None


def report(board_path: pathlib.Path = BOARD, parts_path: pathlib.Path = PARTS,
           graph_path: pathlib.Path | None = GRAPH, description=None) -> dict:
    """The report; `description` (the module's by default) gives the parts' names and the clearance note."""
    joints = (description or DESCRIPTION).manual_joints
    names = joints.get("names", {})
    board = parse(board_path.read_text(encoding="utf-8"))
    parts = json.loads(parts_path.read_text(encoding="utf-8-sig"))["parts"]
    zones = planes(board)
    issues = open_issues(graph_path)
    thickness = board_thickness(board)
    rows = []
    for fp in children(board, "footprint"):
        ref = str(prop(fp, "Reference"))
        pads = [p for p in children(fp, "pad") if len(p) > 2 and p[2] == "thru_hole"]
        if not pads:
            continue
        mpn = prop(fp, "Manufacturer Part")
        spec = parts.get(mpn, {}) if mpn else {}
        pins = spec.get("pins", [])
        attr = child(fp, "attr") or []
        # Not fitted as the board itself says: marked do-not-populate, or left out of the BOM.
        fitted = "dnp" not in attr and "exclude_from_bom" not in attr
        rec = spec.get("recommended_hole")
        kinds: dict[str, dict] = {}
        for pad in pads:
            number = str(pad[1])
            hole = hole_of(pad)
            size = child(pad, "size")
            pin = pin_for(pins, number)
            conn, solid = connection(pad, fp, zones)
            faces = mask_faces(pad)
            pin_fit, cmp = fit(hole, pin), against_recommended(hole, rec)
            row = {
                "ref": ref, "part": part_name(ref, prop(fp, "Value"), names), "mpn": mpn, "fitted": fitted,
                "open_issues": issues_naming(ref, issues),
                "hole": hole_text(hole), "hole_finished_min": hole_text(hole, -TOLERANCE["minus_mm"]),
                "pad_mm": f"{mm(size[1]):.2f} x {mm(size[2]):.2f}", "least_ring_mm": round(least_ring(pad, hole), 3),
                "mask_faces": faces, "mask_ok": len(faces) == 2, "connection": conn, "solid": solid,
                "pin": pin_text(pin), "pin_where": (pin or {}).get("where") or
                ("no pins entry in parts.json" if mpn in parts else f"{mpn} is not in parts.json"),
                "pin_note": (pin or {}).get("note"), "pin_length": length_text(pin, thickness),
                "recommended_hole": recommended_text(rec), "recommended_where": (rec or {}).get("where"),
                "against_recommended": cmp, "pin_verdict": pin_fit["verdict"],
                "margin_mm": pin_fit["margin_mm"], "how": pin_fit["how"],
                "verdict": verdict(fitted, pin_fit, cmp),
            }
            key = json.dumps({k: v for k, v in row.items()}, sort_keys=True, ensure_ascii=False)
            kinds.setdefault(key, {**row, "pads": []})["pads"].append(number)
        rows.extend(kinds.values())
    for row in rows:
        row["holes"] = len(row["pads"])
        row["pads"] = sorted(set(row["pads"]), key=lambda n: (len(n), n))
    counts = {
        "rows": len(rows),
        "does_not_fit": sum(r["verdict"] == DOES_NOT_FIT for r in rows),
        "not_proven": sum(r["verdict"] == NOT_PROVEN for r in rows),
        "as_the_maker_draws_it": sum(r["verdict"] == AS_MAKER for r in rows),
        "not_fitted": sum(r["verdict"] == NOT_FITTED for r in rows),
        "mask_missing": sum(not r["mask_ok"] for r in rows),
        "solid": sum(r["solid"] for r in rows),
    }
    return {"board": board_path.name, "tolerance": TOLERANCE, "recommended_clearance": None,
            "recommended_clearance_note": joints.get("clearance_note", CLEARANCE_NOTE),
            "rules_note": dru_note(board_path), "counts": counts, "rows": rows,
            "pass": not (counts["does_not_fit"] or counts["mask_missing"] or counts["solid"])}


def problems(rep: dict) -> list[str]:
    """Every row that fails the exit status, in words."""
    found = []
    for r in rep["rows"]:
        pads = ", ".join(r["pads"])
        if r["verdict"] == DOES_NOT_FIT:
            if r["pin_verdict"] == DOES_NOT_FIT:
                found.append(f"{r['part']} pads {pads}: pin {r['pin']} does not fit the {r['hole_finished_min']} "
                             f"finished hole (margin {r['margin_mm']} mm)")
            else:
                found.append(f"{r['part']} pads {pads}: the {r['hole']} hole is drawn smaller than the maker's "
                             f"{r['recommended_hole']} ({compare_text(r['against_recommended'])})")
        if not r["mask_ok"]:
            found.append(f"{r['part']} pads {pads}: no mask opening on "
                         f"{', '.join(f for f in ('F', 'B') if f not in r['mask_faces'])}")
        if r["solid"]:
            found.append(f"{r['part']} pads {pads}: joined solid to a plane ({r['connection']})")
    return found


def unproven(rep: dict) -> list[str]:
    return [f"{r['part']} pads {', '.join(r['pads'])}: {r['how']}; no maker's hole either ({r['pin_where']})"
            for r in rep["rows"] if r["verdict"] == NOT_PROVEN]


def verdict_text(r: dict) -> str:
    if r["verdict"] in (FITS, DOES_NOT_FIT) and r["pin_verdict"] == r["verdict"]:
        return f"{r['verdict']}, margin {r['margin_mm']:.3f}"
    if r["verdict"] == NOT_FITTED:
        issues = f" (open issue {', '.join(r['open_issues'])})" if r["open_issues"] else ""
        return f"not fitted{issues}"
    return r["verdict"]


def table(rep: dict) -> list[str]:
    head = ("| Part | Pads | Holes | Hole drawn | Smallest finished | Pad | Least ring | Mask | Zone connection "
            "| Pin | Pin length | Maker's hole | Board against maker's (short / long) | Verdict |")
    lines = [head, "|" + "---|" * 14]
    for r in rep["rows"]:
        mask = "both faces" if r["mask_ok"] else "MISSING on " + ", ".join(f for f in ("F", "B")
                                                                           if f not in r["mask_faces"])
        lines.append(f"| {r['part']} | {', '.join(r['pads'])} | {r['holes']} | {r['hole']} | {r['hole_finished_min']} "
                     f"| {r['pad_mm']} | {r['least_ring_mm']:.3f} | {mask} "
                     f"| {r['connection']}{' SOLID' if r['solid'] else ''} | {r['pin']} | {r['pin_length']} "
                     f"| {r['recommended_hole']} | {compare_text(r['against_recommended'])} | {verdict_text(r)} |")
    return lines


def markdown(rep: dict, parts: str | None = None) -> str:
    """The report as Markdown; `parts` names the parts data it was read from, the description's by default."""
    parts = parts or DESCRIPTION.rel("parts_data")
    c, tol = rep["counts"], rep["tolerance"]
    out = [f"# Hand-soldered joints on {rep['board']}", "",
           f"Result: {'pass' if rep['pass'] else 'FAIL'}. {c['rows']} joint kinds: "
           f"{c['rows'] - c['does_not_fit'] - c['not_proven'] - c['as_the_maker_draws_it'] - c['not_fitted']} fit "
           f"their pin, {c['as_the_maker_draws_it']} as the maker draws it, {c['does_not_fit']} do not fit, "
           f"{c['not_proven']} not proven, {c['not_fitted']} not fitted; {c['mask_missing']} without mask on a face, "
           f"{c['solid']} joined solid.",
           "", "Written by `scripts/board_manual_joints.py` from the board's text, "
           f"`{parts}` and the graph's open issues.", "",
           f"Finished hole: the drawn size +{tol['plus_mm']} / -{tol['minus_mm']} mm ({tol['source']}). The least "
           "ring is at the largest finished hole. A flat pin in a round hole is judged corner to corner; in a slot "
           "both ways, its long side along the slot. Pin sizes add the tolerance the drawing writes on that "
           "dimension; a size written without one is nominal.", "",
           "Verdict: FITS when the whole pin is on record and passes the smallest finished hole; else AS THE MAKER "
           "DRAWS IT when the hole as drawn is at least the maker's recommended hole on both sides; DOES NOT FIT when "
           "the known pin does not pass or the hole is drawn under the maker's; NOT PROVEN when neither a whole pin "
           "nor a maker's hole is on record. The board-against-maker's column gives the drawn hole and the smallest "
           "finished hole minus the maker's, short side / long side. A part the board marks do-not-populate or "
           "leaves out of the BOM is not fitted and not judged.", "",
           f"Recommended clearance: none on record. {rep['recommended_clearance_note']}.", ""]
    if rep["rules_note"]:
        out += [f"Note: {rep['rules_note']}.", ""]
    out += table(rep) + ["", "## How each fit was worked", ""]
    out += [f"- {r['part']}, pads {', '.join(r['pads'])}: {r['how']}." for r in rep["rows"]]
    out += ["", "## Where each pin and maker's hole comes from", ""]
    sources = {}
    for r in rep["rows"]:
        sources.setdefault((r["pin_where"], r["pin_note"], r["recommended_where"]), []).append(r["ref"])
    for (where, note, rec), refs in sources.items():
        out.append(f"- {', '.join(dict.fromkeys(refs))}: pin, {where}." + (f" {note}." if note else "")
                   + (f" Maker's hole, {rec}." if rec else ""))
    found, open_ = problems(rep), unproven(rep)
    out += ["", "## Failing", ""] + ([f"- {f}" for f in found] or ["None."])
    out += ["", "## Not proven", ""] + ([f"- {f}" for f in open_] or ["None."])
    return "\n".join(out).replace("—", ",") + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--board", type=pathlib.Path, help="the board; the description's pcb by default")
    parser.add_argument("--parts", type=pathlib.Path, help="the parts data; the description's parts_data by default")
    parser.add_argument("--write", type=pathlib.Path, help="write the table as Markdown")
    parser.add_argument("--json", type=pathlib.Path, help="write the report as JSON")
    board_description.add_argument(parser)
    args = parser.parse_args()
    description = board_description.load(args.board_description) if args.board_description else DESCRIPTION
    rep = report(args.board or description.path("pcb"), args.parts or description.path("parts_data"),
                 description=description)
    text = markdown(rep, description.rel("parts_data"))
    sys.stdout.buffer.write(text.encode("utf-8"))
    if args.write:
        args.write.parent.mkdir(parents=True, exist_ok=True)
        args.write.write_bytes(text.encode("utf-8"))  # LF on every machine; write_text's newline= needs Python 3.10
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(rep, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0 if rep["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
