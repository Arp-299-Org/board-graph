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
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import board_description  # noqa: E402
import kicad_sexp  # noqa: E402
import tool_paths  # noqa: E402
from no_window import KWARGS as NO_WINDOW  # noqa: E402

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


def normalize_pcb(board_path: pathlib.Path) -> bool:
    """Save `board_path` once via offline pcbnew so KiCad 10 boilerplate/setup blocks are settled."""
    tools_dir = TOOL_ROOT / "tools" / "kicad"
    if str(tools_dir) not in sys.path:
        sys.path.insert(0, str(tools_dir))
    import kicad_install  # noqa: E402

    kicad_py = getattr(kicad_install, "KICAD_PYTHON", None)
    if not isinstance(kicad_py, pathlib.Path) or not kicad_py.is_file():
        return False
    code = "import sys, pcbnew; p = sys.argv[1]; b = pcbnew.LoadBoard(p); pcbnew.SaveBoard(p, b)"
    done = subprocess.run([str(kicad_py), "-B", "-c", code, str(board_path)], capture_output=True, text=True, **NO_WINDOW)
    if done.returncode != 0:
        raise RuntimeError(done.stderr.strip() or done.stdout.strip() or f"pcbnew normalize failed (exit {done.returncode})")
    return True


def _is_hidden_text(node: list) -> bool:
    if any(part == "hide" or part == ["hide", "yes"] for part in node[2:]):
        return True
    eff = kicad_sexp.child(node, "effects")
    return bool(eff and any(part == "hide" or part == ["hide", "yes"] for part in eff[1:]))


def _check_silk_node(node: list, prefix: str, min_height_mm: float, min_thickness_mm: float) -> str | None:
    layer = kicad_sexp.child(node, "layer")
    layer_name = str(layer[1]) if layer and len(layer) > 1 else ""
    if layer_name not in ("F.SilkS", "B.SilkS") or _is_hidden_text(node):
        return None
    text = str(node[2] if node[0] in ("fp_text", "property") and len(node) > 2 else (node[1] if len(node) > 1 else "")).strip()
    if not text:
        return None
    eff = kicad_sexp.child(node, "effects")
    font = kicad_sexp.child(eff, "font") if eff else None
    size = kicad_sexp.child(font, "size") if font else None
    thick = kicad_sexp.child(font, "thickness") if font else None
    try:
        h = min(float(size[1]), float(size[2])) if size and len(size) > 2 else 0.0
        t = float(thick[1]) if thick and len(thick) > 1 else 0.0
    except (TypeError, ValueError):
        return None
    if h + 1e-6 < min_height_mm or t + 1e-6 < min_thickness_mm:
        return f"{prefix} '{text}' on {layer_name}: size {h:.2f} mm / stroke {t:.2f} mm below {min_height_mm:.2f}/{min_thickness_mm:.2f} mm"
    return None


def silkscreen_minimum_findings(board_text: str, min_height_mm: float = 1.0, min_thickness_mm: float = 0.15) -> list[str]:
    """Flag visible silkscreen labels below JLCPCB's legible height or stroke-width minimums."""
    tree = kicad_sexp.parse(board_text)
    findings: list[str] = []
    for item in kicad_sexp.children(tree, "gr_text"):
        msg = _check_silk_node(item, "gr_text", min_height_mm, min_thickness_mm)
        if msg:
            findings.append(msg)
    for fp in kicad_sexp.children(tree, "footprint"):
        ref = _footprint_ref(fp) or "?"
        for tag in ("fp_text", "property"):
            for node in kicad_sexp.children(fp, tag):
                msg = _check_silk_node(node, ref, min_height_mm, min_thickness_mm)
                if msg:
                    findings.append(msg)
    return findings


SILK_DRC_TYPES = ("text_height", "text_thickness")


def parse_silkscreen_drc_report(report: dict) -> list[str]:
    """Extract silkscreen overlap/clearance/font DRC violations while ignoring pre-routing opens."""
    findings: list[str] = []
    for item in report.get("violations", []):
        if not isinstance(item, dict):
            continue
        vtype = str(item.get("type") or "")
        if not (vtype.startswith("silk_") or vtype in SILK_DRC_TYPES):
            continue
        desc = str(item.get("description") or vtype).strip()
        where = "; ".join(
            str(part.get("description") or "").strip()
            for part in item.get("items", [])
            if isinstance(part, dict) and part.get("description")
        )
        findings.append(f"{vtype}: {desc}" + (f" ({where})" if where else ""))
    return findings


def silkscreen_drc_findings(board_path: pathlib.Path) -> list[str]:
    """Run KiCad DRC on `board_path` if available and return any silkscreen violations."""
    invoke_cli = TOOL_ROOT / "tools" / "kicad" / "invoke_kicad_cli.py"
    if not invoke_cli.is_file():
        return []
    with tempfile.TemporaryDirectory() as tmp:
        out_json = pathlib.Path(tmp) / "drc.json"
        cmd = [
            sys.executable, "-B", str(invoke_cli),
            "pcb", "drc", "--format", "json", "--severity-all",
            "--output", str(out_json), str(board_path),
        ]
        done = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, check=False, **NO_WINDOW)
        if done.returncode != 0 or not out_json.is_file():
            return []
        try:
            report = json.loads(out_json.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
    return parse_silkscreen_drc_report(report)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board", type=pathlib.Path, help="Input .kicad_pcb file")
    parser.add_argument("--out", type=pathlib.Path, help="Output owner_placement.json file")
    parser.add_argument("--render-dir", type=pathlib.Path, help="Render output directory")
    parser.add_argument("--no-render", action="store_true", help="Skip rendering 2D/3D review views")
    parser.add_argument("--no-normalize", action="store_true", help="Skip KiCad 10 load/save normalization")
    parser.add_argument("--normalize-only", action="store_true", help="Normalize PCB to KiCad 10 and exit")
    parser.add_argument("--no-drc", action="store_true", help="Skip KiCad silkscreen DRC overlap check")
    parser.add_argument("--strict-silk", action="store_true", help="Exit 1 if any silkscreen text is undersized")
    parser.add_argument("--min-silk-height", type=float, default=1.0, help="Min silkscreen height mm (default 1.0)")
    parser.add_argument("--min-silk-thickness", type=float, default=0.15, help="Min silkscreen stroke mm (default 0.15)")
    default_quality = "basic" if os.environ.get("LIBGL_ALWAYS_SOFTWARE") == "1" else "high"
    parser.add_argument("--quality", choices=("basic", "high"), default=default_quality)
    board_description.add_argument(parser)
    args = parser.parse_args(argv)

    desc = board_description.load(args.board_description, root=ROOT) if args.board_description else board_description.load(root=ROOT)
    board = (args.board or desc.path("pcb")).resolve()
    out = (args.out or desc.path("owner_placement")).resolve()
    render_dir = (args.render_dir or (desc.path("reports") / "placement")).resolve()

    if not args.no_normalize:
        normalized = normalize_pcb(board)
        if normalized:
            print(f"normalized {board} via offline pcbnew")
    if args.normalize_only:
        return 0

    board_text = board.read_text(encoding="utf-8")
    placement = extract_placement(board_text)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(placement, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out} ({len(placement['parts'])} part(s))")

    silk_warnings = silkscreen_minimum_findings(board_text, args.min_silk_height, args.min_silk_thickness)
    if not args.no_drc:
        silk_warnings.extend(silkscreen_drc_findings(board))
    for warning in silk_warnings:
        print(f"warning: silkscreen {warning}", file=sys.stderr)
    if args.strict_silk and silk_warnings:
        return 1

    if not args.no_render:
        renderer = TOOL_ROOT / "tools" / "kicad" / "render_board_views.py"
        cmd = [sys.executable, "-B", str(renderer), "--board", str(board), "--out", str(render_dir), "--quality", args.quality]
        done = subprocess.run(cmd, cwd=str(ROOT), check=False)
        if done.returncode != 0:
            return done.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
