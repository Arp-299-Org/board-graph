#!/usr/bin/env python3
"""Generate AP214 STEP 3D models from simple box/cylinder/d_shaft specs and optionally sync to PCB."""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import board_description  # noqa: E402
import board_sync_models  # noqa: E402
import tool_paths  # noqa: E402

if __name__ == "__main__":
    tool_paths.take_argument()

ROOT = tool_paths.project_root()

COLORS: dict[str, tuple[float, float, float]] = {
    "black": (0.10, 0.10, 0.10),
    "tin": (0.82, 0.82, 0.84),
    "silver": (0.82, 0.82, 0.84),
    "ivory": (0.93, 0.89, 0.78),
    "white": (0.94, 0.94, 0.94),
    "ceramic": (0.72, 0.60, 0.42),
    "gold": (0.83, 0.69, 0.22),
    "green": (0.14, 0.45, 0.22),
}


def _resolve_color(value) -> tuple[float, float, float]:
    if isinstance(value, str):
        key = value.strip().lower()
        if key not in COLORS:
            raise ValueError(f"unknown color {value!r}; choose {', '.join(sorted(COLORS))}")
        return COLORS[key]
    if isinstance(value, (list, tuple)) and len(value) == 3:
        return tuple(float(c) for c in value)  # type: ignore[return-value]
    raise ValueError(f"invalid color {value!r}")


def _parse_vec3(text: str) -> tuple[float, float, float]:
    parts = [float(x.strip()) for x in text.split(",")]
    if len(parts) != 3:
        raise ValueError(f"expected X,Y,Z numbers, got {text!r}")
    return parts[0], parts[1], parts[2]


def build_step_from_spec(name: str, solids: list[dict], project_name: str = "board-graph") -> str:
    """Build AP214 STEP text from a list of box/cylinder/d_shaft solid dictionaries."""
    import kicad_step_model

    old_project = kicad_step_model.PROJECT
    kicad_step_model.PROJECT = project_name
    try:
        step = kicad_step_model.Step(name)
        for item in solids:
            kind = str(item.get("type") or "box").lower()
            colour = _resolve_color(item.get("color", "black"))
            if kind == "box":
                if "bounds" in item:
                    x0, y0, z0, x1, y1, z1 = (float(v) for v in item["bounds"])
                else:
                    sx, sy, sz = (float(v) for v in item["size"])
                    ox, oy, oz = (float(v) for v in item.get("offset", (0.0, 0.0, 0.0)))
                    x0, x1 = ox - sx / 2.0, ox + sx / 2.0
                    y0, y1 = oy - sy / 2.0, oy + sy / 2.0
                    z0, z1 = oz, oz + sz
                poly, pz0, pz1, col = kicad_step_model.box(x0, y0, z0, x1, y1, z1, colour)
            elif kind == "cylinder":
                cx, cy = (float(v) for v in item.get("center", (0.0, 0.0)))
                poly, pz0, pz1, col = kicad_step_model.cylinder(
                    cx, cy, float(item["radius"]), float(item["z0"]), float(item["z1"]), colour
                )
            elif kind == "d_shaft":
                cx, cy = (float(v) for v in item.get("center", (0.0, 0.0)))
                poly, pz0, pz1, col = kicad_step_model.d_shaft(
                    cx, cy, float(item["radius"]), float(item["flat"]), float(item["z0"]), float(item["z1"]), colour
                )
            else:
                raise ValueError(f"unsupported solid type {kind!r}")
            step.prism(poly, pz0, pz1, col)
        return step.text()
    finally:
        kicad_step_model.PROJECT = old_project


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=pathlib.Path, help="JSON file mapping {model_name: [solid, ...]}")
    parser.add_argument("--name", help="Single STEP model stem name (with --size)")
    parser.add_argument("--size", help="Box size X,Y,Z in mm (for --name)")
    parser.add_argument("--offset", default="0,0,0", help="Box center offset OX,OY,Z0 in mm (default 0,0,0)")
    parser.add_argument("--color", default="black", help="Named color (black, tin, ivory, gold, ...)")
    parser.add_argument("--out", type=pathlib.Path, help="Output models folder (default from board.json)")
    parser.add_argument("--sync", action="store_true", help="Run board-graph sync-models after writing STEP file(s)")
    board_description.add_argument(parser)
    args = parser.parse_args(argv)

    desc = board_description.load(args.board_description, root=ROOT) if args.board_description else board_description.load(root=ROOT)
    model_dirs = desc.paths("model_folders")
    out_dir = (args.out or (model_dirs[0] if model_dirs else desc.path("pcb").parent / "models")).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        if args.spec:
            specs = json.loads(args.spec.resolve().read_text(encoding="utf-8"))
            if not isinstance(specs, dict) or not specs:
                raise ValueError("spec JSON must map {model_name: [solid, ...]}")
        elif args.name and args.size:
            sx, sy, sz = _parse_vec3(args.size)
            ox, oy, oz = _parse_vec3(args.offset)
            specs = {args.name: [{"type": "box", "size": [sx, sy, sz], "offset": [ox, oy, oz], "color": args.color}]}
        else:
            raise ValueError("pass either --spec <file.json> or --name <NAME> --size <X,Y,Z>")
        for model_name, solids in specs.items():
            dest = out_dir / f"{model_name}.step"
            dest.write_text(build_step_from_spec(model_name, solids, desc["project"]), encoding="utf-8")
            print(f"wrote {dest} ({len(solids)} solid(s))")
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"board-graph make-step: {error}", file=sys.stderr)
        return 2

    if args.sync:
        sync_args = ["--models-dir", str(out_dir)]
        if args.board_description:
            sync_args += ["--board-description", str(args.board_description)]
        return board_sync_models.main(sync_args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
