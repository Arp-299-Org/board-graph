#!/usr/bin/env python3
"""Report each part's 3D model extent along the board's height axis (Z), from saved files.

Reads the board its description names (scripts/board_description.py), or --board, and the STEP models beside it. No KiCad, GUI or board writes.
Z is 0 at the face the part mounts on and grows away from the board. Each point is first moved by
its footprint's saved model transform (scale, then rotation about X, Y and Z, then offset), as KiCad
draws it. The footprint's own angle turns about Z and a back-face part is flipped so its Z grows
out of the back face, so neither changes a height from the mount face. Construction points can
sit far from the solid: a peak held by a few points, with a wide gap below it, is reported as
stray and left out of the body. A STEP point list is evidence about the model, not the part:
check a datasheet before relying on a number.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import pathlib
import re
import sys

_SCRIPTS = pathlib.Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
import tool_paths  # noqa: E402

if __name__ == "__main__":
    tool_paths.take_argument()

import board_description  # noqa: E402
from kicad_sexp import child, children, parse, prop  # noqa: E402

# Where KiCad 10 installs its own models on the Mac, Windows, and Linux.
STOCK_MODEL_INSTALLS = (pathlib.Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport/3dmodels"),
                        pathlib.Path(r"C:\Program Files\KiCad\10.0\share\kicad\3dmodels"),
                        pathlib.Path("/usr/share/kicad/3dmodels"))
# KiCad's own models: the variable when set, otherwise where KiCad 10 installs them.
STOCK_MODELS = [pathlib.Path(p) for p in (os.environ.get("KICAD10_3DMODEL_DIR", ""),) if p] + list(STOCK_MODEL_INSTALLS)
POINT = re.compile(r"CARTESIAN_POINT\s*\(\s*'[^']*'\s*,\s*\(\s*([-\dE.+]+)\s*,\s*([-\dE.+]+)\s*,\s*([-\dE.+]+)\s*\)")
NUMBERED_POINT = re.compile(r"#(\d+)\s*=\s*CARTESIAN_POINT\s*\(\s*'[^']*'\s*,\s*\(\s*([-\dE.+]+)\s*,\s*([-\dE.+]+)\s*,\s*([-\dE.+]+)\s*\)")
VERTEX = re.compile(r"VERTEX_POINT\s*\(\s*'[^']*'\s*,\s*#(\d+)\s*\)")
STRAY_MAX_POINTS = 8   # a real top face has more points than this
STRAY_MIN_GAP_MM = 2.0

# The transformed sampler reads coordinates, not STEP surfaces or a solid model.
# KiCad 10 applies T(offset) * Rz(-z) * Ry(-y) * Rx(-x) * S(scale):
# https://gitlab.com/kicad/code/kicad/-/raw/10.0/3d-viewer/3d_rendering/raytracing/create_scene.cpp
SAMPLE_LIMITS = [
    "Sampled STEP points are not solid extrema; curved surfaces can extend beyond vertices.",
    "STEP representation placement and solid topology are not reconstructed; coordinates are used as written.",
    "Datasheet interpretation and physical part qualification are not established.",
]
_REAL = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?"
_LABEL = r"'(?:[^']|'')*'"
_POINT_XYZ = re.compile(
    rf"#\s*(\d+)\s*=\s*CARTESIAN_POINT\s*\(\s*{_LABEL}\s*,\s*\(\s*({_REAL})\s*,\s*({_REAL})\s*,\s*({_REAL})\s*\)\s*\)",
    re.IGNORECASE,
)
_POINT_IDS = re.compile(r"#\s*(\d+)\s*=\s*CARTESIAN_POINT\s*\(", re.IGNORECASE)
_VERTEX_REFS = re.compile(rf"VERTEX_POINT\s*\(\s*{_LABEL}\s*,\s*#\s*(\d+)\s*\)", re.IGNORECASE)
_VERTEX_COUNT = re.compile(r"\bVERTEX_POINT\s*\(", re.IGNORECASE)
_MILLIMETRES = re.compile(r"\bSI_UNIT\s*\(\s*\.MILLI\.\s*,\s*\.METRE\.\s*\)", re.IGNORECASE)
_STEP_PLACEMENT = re.compile(
    r"\b(?:ITEM_DEFINED_TRANSFORMATION|REPRESENTATION_RELATIONSHIP_WITH_TRANSFORMATION|"
    r"MAPPED_ITEM|CARTESIAN_TRANSFORMATION_OPERATOR(?:_3D)?)\s*\(", re.IGNORECASE,
)


def transform_point(point, scale, rotate, offset):
    """Apply KiCad's saved model transform in footprint-local millimetres."""
    vectors = (point, scale, rotate, offset)
    if any(len(v) != 3 or not all(math.isfinite(float(n)) for n in v) for v in vectors):
        raise ValueError("A model transform needs finite XYZ values")
    x, y, z = (float(point[i]) * float(scale[i]) for i in range(3))
    ax, ay, az = (math.radians(-float(angle)) for angle in rotate)
    y, z = y * math.cos(ax) - z * math.sin(ax), y * math.sin(ax) + z * math.cos(ax)
    x, z = x * math.cos(ay) + z * math.sin(ay), -x * math.sin(ay) + z * math.cos(ay)
    x, y = x * math.cos(az) - y * math.sin(az), x * math.sin(az) + y * math.cos(az)
    return x + float(offset[0]), y + float(offset[1]), z + float(offset[2])


def measure_model(path: pathlib.Path, *, scale, rotate, offset) -> dict:
    """Sample transformed STEP points. This is not a solid-bounding-box calculation."""
    result = {"status": "unavailable", "source_sha256": None, "method": None, "count": 0,
              "z_min_mm": None, "z_max_mm": None, "limits": list(SAMPLE_LIMITS)}
    try:
        transform_point((0, 0, 0), scale, rotate, offset)
        if path.suffix.lower() not in (".step", ".stp"):
            raise ValueError("Only STEP model files can be sampled")
        data = path.read_bytes()
        result["source_sha256"] = hashlib.sha256(data).hexdigest()
        if b"\0" in data or data.startswith(b"version https://git-lfs.github.com/spec/"):
            raise ValueError("Model is not an available text STEP file")
        text = data.decode("utf-8", errors="replace")
        if not text.lstrip().upper().startswith("ISO-10303-21;") or "END-ISO-10303-21;" not in text.upper():
            raise ValueError("Model is not a complete text STEP file")
        length_units = [record for record in text.split(";") if re.search(r"\bLENGTH_UNIT\s*\(", record, re.IGNORECASE)]
        if not length_units or any(not _MILLIMETRES.search(record) for record in length_units):
            raise ValueError("STEP length units are missing, mixed, or not explicitly millimetres")
        if _STEP_PLACEMENT.search(text):
            raise ValueError("STEP contains an unsupported representation or assembly transform")
        identifiers = _POINT_IDS.findall(text)
        matches = _POINT_XYZ.findall(text)
        if len(identifiers) != len(set(identifiers)) or len(matches) != len(identifiers):
            raise ValueError("STEP Cartesian point definitions are incomplete or ambiguous")
        points = {number: tuple(float(value) for value in values) for number, *values in matches}
        if any(not all(math.isfinite(v) for v in point) for point in points.values()):
            raise ValueError("STEP contains non-finite coordinates")
        vertices = _VERTEX_REFS.findall(text)
        if len(vertices) != len(_VERTEX_COUNT.findall(text)) or any(ref not in points for ref in vertices):
            raise ValueError("STEP vertex references are incomplete or unresolved")
        if vertices:
            selected = [points[ref] for ref in sorted(set(vertices), key=int)]
            method = "step_vertex_points"
        else:
            selected = list(points.values())
            method = "step_cartesian_points"
            result["limits"].append("Cartesian samples can include construction points unrelated to the solid.")
        if not selected:
            raise ValueError("No STEP points could be sampled")
        heights = [transform_point(point, scale, rotate, offset)[2] for point in selected]
        if not all(math.isfinite(value) for value in heights):
            raise ValueError("Transformed coordinates are not finite")
        result.update(status="sampled", method=method, count=len(selected),
                      z_min_mm=round(min(heights), 6), z_max_mm=round(max(heights), 6))
    except (OSError, ValueError, TypeError, OverflowError) as error:
        result["reason"] = str(error)
    return result


def body_extent(values: list[float]) -> tuple[float, float]:
    """The solid's top, leaving out a few construction points far above it, and the absolute maximum."""
    ordered = sorted(values)
    peak = ordered[-1]
    near_peak = [v for v in ordered if peak - v < STRAY_MIN_GAP_MM]
    if len(near_peak) <= STRAY_MAX_POINTS and len(near_peak) < len(ordered):
        return ordered[-len(near_peak) - 1], peak
    return peak, peak


def model_height(path: pathlib.Path, *, scale, rotate, offset) -> dict | None:
    """The model's extent along Z once its saved transform is applied."""
    def height(xyz):
        return transform_point([float(v) for v in xyz], scale, rotate, offset)[2]

    text = path.read_text(encoding="utf-8", errors="replace")
    # Corner points (VERTEX_POINT) lie on the solid; axis and surface placements need not.
    corners = set(VERTEX.findall(text))
    numbered = {n: xyz for n, *xyz in NUMBERED_POINT.findall(text)}
    chosen = [height(numbered[n]) for n in corners if n in numbered]
    if chosen:
        return {"bottom_mm": round(min(chosen), 2), "body_top_mm": round(max(chosen), 2), "peak_mm": round(max(chosen), 2)}
    points = [height(p) for p in POINT.findall(text)]
    if not points:
        return None
    body, peak = body_extent(points)
    return {"bottom_mm": round(min(points), 2), "body_top_mm": round(body, 2), "peak_mm": round(peak, 2)}


def _model_vector(model, name, default):
    part = child(model, name)
    if part is None:
        return list(default)
    xyz = child(part, "xyz")
    if xyz is None or len(xyz) != 4:
        raise ValueError(f"Model {name} needs three XYZ values")
    values = [float(value) for value in xyz[1:]]
    if not all(math.isfinite(value) for value in values):
        raise ValueError(f"Model {name} contains non-finite values")
    return values


def _snapshot_model_path(board, saved, path):
    if not (saved.startswith("${KIPRJMOD}/") or saved.startswith("${KICAD10_3DMODEL_DIR}/")):
        raise ValueError("Model path is not a supported project or pinned stock path")
    root = board.resolve().parent.parent
    if not path.resolve().is_relative_to(root):
        raise ValueError("Model path escapes the project snapshot")
    # Walk the path as given and stop at the snapshot as given: on a Mac the resolved root
    # sits under /private/var while the given paths say /var, which is itself a symlink.
    snapshot = board.parent.parent
    for part in (path, *path.parents):
        if part == snapshot:
            break
        if part.is_symlink():
            raise ValueError("Model path uses a symlink")
    return path


def report(board: pathlib.Path, *, references=None, transformed=False) -> list[dict]:
    rows = []
    wanted = None if references is None else set(references)
    seen = set()
    for fp in children(parse(board.read_text(encoding="utf-8")), "footprint"):
        reference = prop(fp, "Reference")
        if wanted is not None and reference not in wanted:
            continue
        if wanted is not None and reference in seen:
            raise ValueError(f"Duplicate requested footprint reference: {reference}")
        seen.add(reference)
        row = {"ref": reference, "face": "front" if child(fp, "layer")[1] == "F.Cu" else "back", "model": None}
        model = child(fp, "model")
        if transformed and (len(children(fp, "model")) != 1 or len(model) < 2 or not isinstance(model[1], str)):
            row["model_path"] = None
            row["point_measurement"] = {"status": "unavailable", "source_sha256": None, "method": None,
                                        "count": 0, "z_min_mm": None, "z_max_mm": None,
                                        "reason": "Expected exactly one saved 3D model", "limits": list(SAMPLE_LIMITS)}
            rows.append(row)
            continue
        if model:
            path = pathlib.Path(model[1].replace("${KIPRJMOD}", str(board.parent)))
            if model[1].startswith("${KICAD10_3DMODEL_DIR}"):
                rel = model[1].split("}", 1)[1].lstrip("/")
                path = next((d / rel for d in STOCK_MODELS if (d / rel).exists()),
                            STOCK_MODELS[-1] / rel if STOCK_MODELS else pathlib.Path(model[1]))
            if transformed:
                row["model"] = path.name
                row["model_path"] = model[1]
                try:
                    if child(model, "at") is not None:
                        raise ValueError("Legacy model at-offset units are not supported")
                    scale = _model_vector(model, "scale", (1, 1, 1))
                    rotate = _model_vector(model, "rotate", (0, 0, 0))
                    offset = _model_vector(model, "offset", (0, 0, 0))
                    row["transform"] = {"scale_xyz": scale, "rotate_deg_xyz": rotate, "offset_mm_xyz": offset}
                    path = _snapshot_model_path(board, model[1], path)
                    hidden = children(model, "hide")
                    if "hide" in model[2:] or (hidden and hidden != [["hide", "no"]]):
                        raise ValueError("Saved 3D model is hidden or its visibility is unsupported")
                    row["point_measurement"] = measure_model(path, scale=scale, rotate=rotate, offset=offset)
                except (ValueError, TypeError) as error:
                    row["point_measurement"] = {"status": "unavailable", "source_sha256": None, "method": None,
                                                "count": 0, "z_min_mm": None, "z_max_mm": None,
                                                "reason": str(error), "limits": list(SAMPLE_LIMITS)}
                rows.append(row)
                continue
            row["model"] = path.name
            try:
                if child(model, "at") is not None:
                    raise ValueError("legacy model at-offset units are not supported")
                transform = {"scale": _model_vector(model, "scale", (1, 1, 1)),
                             "rotate": _model_vector(model, "rotate", (0, 0, 0)),
                             "offset": _model_vector(model, "offset", (0, 0, 0))}
                if path.exists():
                    row.update(model_height(path, **transform) or {"note": "no points read"})
                else:
                    row["note"] = "model file missing"
            except (ValueError, TypeError) as error:
                row["note"] = f"model not measured: {error}"
        rows.append(row)
    if wanted is not None and seen != wanted:
        raise ValueError("Requested footprint references are missing: " + ", ".join(sorted(wanted - seen)))
    return sorted(rows, key=lambda r: (r["face"], -r.get("body_top_mm", -1), r["ref"]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board", type=pathlib.Path, help="the board file; the description's pcb by default")
    board_description.add_argument(parser)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    rows = report(args.board or board_description.load(args.board_description).path("pcb"))
    if args.json:
        print(json.dumps(rows, indent=2))
        return
    for row in rows:
        stray = f"  stray to {row['peak_mm']}" if row.get("peak_mm", 0) - row.get("body_top_mm", 0) > 0.5 else ""
        height = f"{row['body_top_mm']:6.2f} mm" if "body_top_mm" in row else "     none"
        print(f"{row['face']:6}{row['ref']:7}{height}{stray}  {row.get('note', row['model'] or 'no model')}")


if __name__ == "__main__":
    main()
