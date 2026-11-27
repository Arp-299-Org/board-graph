#!/usr/bin/env python3
"""Write board/owner_placement.json from the saved PCB and optionally render review PNGs."""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import board_description  # noqa: E402
import kicad_sexp  # noqa: E402
import tool_paths  # noqa: E402

if __name__ == "__main__":
    tool_paths.take_argument()

TOOL_ROOT = tool_paths.TOOL_ROOT
ROOT = tool_paths.project_root()


def _ref_sort_key(ref: str) -> tuple[str, int, str]:
    match = re.match(r"^([A-Za-z_]+)(\d+)(.*)$", ref)
    return (match.group(1), int(match.group(2)), match.group(3)) if match else (ref, 0, "")


def _footprint_ref(fp: list) -> str:
    ref = kicad_sexp.prop(fp, "Reference")
    if ref:
        return str(ref).strip()
    for txt in kicad_sexp.children(fp, "fp_text"):
        if len(txt) >= 3 and str(txt[1]) == "reference":
            return str(txt[2]).strip()
    return ""


def extract_placement(board_text: str) -> dict:
    """Extract {parts: {ref: {x, y, rotation, side}}} compatible with board_graph_actions.placement_findings."""
    root = kicad_sexp.parse(board_text)
    parts: dict[str, dict] = {}
    for fp in kicad_sexp.children(root, "footprint"):
        ref = _footprint_ref(fp)
        if not ref:
            continue
        at = kicad_sexp.child(fp, "at")
        layer = kicad_sexp.child(fp, "layer")
        if not at or len(at) < 3 or not layer or len(layer) < 2:
            continue
        x = round(float(at[1]), 4)
        y = round(float(at[2]), 4)
        rot = round(float(at[3]) if len(at) > 3 else 0.0, 4) % 360.0
        side = "front" if str(layer[1]) == "F.Cu" else ("back" if str(layer[1]) == "B.Cu" else str(layer[1]))
        parts[ref] = {"x": x, "y": y, "rotation": rot, "side": side}
    return {"parts": {ref: parts[ref] for ref in sorted(parts, key=_ref_sort_key)}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board", type=pathlib.Path, help="Input .kicad_pcb file")
    parser.add_argument("--out", type=pathlib.Path, help="Output owner_placement.json file")
    parser.add_argument("--render-dir", type=pathlib.Path, help="Render output directory")
    parser.add_argument("--no-render", action="store_true", help="Skip rendering 2D/3D review views")
    default_quality = "basic" if os.environ.get("LIBGL_ALWAYS_SOFTWARE") == "1" else "high"
    parser.add_argument("--quality", choices=("basic", "high"), default=default_quality)
    board_description.add_argument(parser)
    args = parser.parse_args(argv)

    desc = board_description.load(args.board_description, root=ROOT) if args.board_description else board_description.load(root=ROOT)
    board = (args.board or desc.path("pcb")).resolve()
    out = (args.out or desc.path("owner_placement")).resolve()
    render_dir = (args.render_dir or (desc.path("reports") / "placement")).resolve()

    placement = extract_placement(board.read_text(encoding="utf-8"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(placement, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out} ({len(placement['parts'])} part(s))")

    if not args.no_render:
        renderer = TOOL_ROOT / "tools" / "kicad" / "render_board_views.py"
        cmd = [sys.executable, "-B", str(renderer), "--board", str(board), "--out", str(render_dir), "--quality", args.quality]
        done = subprocess.run(cmd, cwd=str(ROOT), check=False)
        if done.returncode != 0:
            return done.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
