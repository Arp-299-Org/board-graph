#!/usr/bin/env python3
"""Attach matching STEP 3D models to board footprints offline and sync .pretty/."""
from __future__ import annotations

import argparse
import json
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
from no_window import KWARGS as NO_WINDOW  # noqa: E402

if __name__ == "__main__":
    tool_paths.take_argument()

TOOL_ROOT = tool_paths.TOOL_ROOT
ROOT = tool_paths.project_root()


def _kicad_python() -> pathlib.Path | None:
    tools_dir = TOOL_ROOT / "tools" / "kicad"
    if str(tools_dir) not in sys.path:
        sys.path.insert(0, str(tools_dir))
    import kicad_install  # noqa: E402

    found = getattr(kicad_install, "KICAD_PYTHON", None)
    return found if isinstance(found, pathlib.Path) and found.is_file() else None


def _norm_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def match_model_file(models_dir: pathlib.Path, mpn: str = "", fp_name: str = "") -> pathlib.Path | None:
    """Find a STEP/STP file under `models_dir` matching `mpn` or footprint item name."""
    if not models_dir.is_dir():
        return None
    candidates = [p for p in sorted(models_dir.iterdir()) if p.is_file() and p.suffix.lower() in (".step", ".stp")]
    if not candidates:
        return None
    keys = [_norm_key(val) for val in (mpn, fp_name) if val]
    for key in keys:
        for path in candidates:
            if _norm_key(path.stem) == key:
                return path
    return None


def plan_model_bindings(board_path: pathlib.Path, models_dir: pathlib.Path) -> list[dict]:
    """Return [{ref, fp_name, model_rel}] for board footprints matching a STEP file in `models_dir`."""
    tree = kicad_sexp.parse(board_path.read_text(encoding="utf-8"))
    bindings: list[dict] = []
    for fp in kicad_sexp.children(tree, "footprint"):
        ref = str(kicad_sexp.prop(fp, "Reference") or "").strip()
        if not ref:
            continue
        raw_id = str(fp[1]) if len(fp) > 1 else ""
        fp_name = raw_id.split(":", 1)[-1]
        mpn = str(kicad_sexp.prop(fp, ("Manufacturer Part", "MPN")) or kicad_sexp.prop(fp, "Value") or "").strip()
        matched = match_model_file(models_dir, mpn=mpn, fp_name=fp_name)
        if matched is None:
            continue
        bindings.append({
            "ref": ref,
            "fp_name": fp_name,
            "model_path": f"${{KIPRJMOD}}/{models_dir.name}/{matched.name}",
            "existing_models": [str(m[1]) for m in kicad_sexp.children(fp, "model") if len(m) > 1],
        })
    return bindings


def _apply_models_in_kicad_python(kicad_py: pathlib.Path, board_path: pathlib.Path, bindings: list[dict]) -> None:
    payload = json.dumps([{"ref": b["ref"], "model_path": b["model_path"]} for b in bindings])
    code = (
        "import json, sys, pcbnew\n"
        "board_file, raw = sys.argv[1], json.loads(sys.argv[2])\n"
        "board = pcbnew.LoadBoard(board_file)\n"
        "changed = False\n"
        "for item in raw:\n"
        "    fp = board.FindFootprintByReference(item['ref'])\n"
        "    if fp is None:\n"
        "        continue\n"
        "    want = item['model_path']\n"
        "    have = [m.m_Filename for m in fp.Models()]\n"
        "    if have != [want]:\n"
        "        fp.Models().clear()\n"
        "        m = pcbnew.FP_3DMODEL()\n"
        "        m.m_Filename = want\n"
        "        fp.Add3DModel(m)\n"
        "        changed = True\n"
        "pcbnew.SaveBoard(board_file, board)\n"
        "print(f'synced {len(raw)} footprint(s), updated={changed}')\n"
    )
    done = subprocess.run([str(kicad_py), "-B", "-c", code, str(board_path), payload], capture_output=True, text=True, **NO_WINDOW)
    if done.returncode != 0:
        raise RuntimeError(done.stderr.strip() or done.stdout.strip() or f"exit {done.returncode}")
    if done.stdout.strip():
        print(done.stdout.strip())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board", type=pathlib.Path, help="Target .kicad_pcb file")
    parser.add_argument("--models-dir", type=pathlib.Path, help="Models folder (default from board.json)")
    parser.add_argument("--no-lib-sync", action="store_true", help="Skip syncing back to .pretty/")
    board_description.add_argument(parser)
    args = parser.parse_args(argv)

    desc = board_description.load(args.board_description, root=ROOT) if args.board_description else board_description.load(root=ROOT)
    board_path = (args.board or desc.path("pcb")).resolve()
    model_dirs = desc.paths("model_folders")
    models_dir = (args.models_dir or (model_dirs[0] if model_dirs else board_path.parent / "models")).resolve()
    kicad_py = _kicad_python()
    if kicad_py is None:
        print("KiCad Python not found; cannot run offline pcbnew model sync", file=sys.stderr)
        return 1

    bindings = plan_model_bindings(board_path, models_dir)
    if not bindings:
        print(f"no matching STEP models found under {models_dir}")
        return 1
    _apply_models_in_kicad_python(kicad_py, board_path, bindings)
    if not args.no_lib_sync:
        sync_script = TOOL_ROOT / "scripts" / "kicad_footprints_to_library.py"
        done = subprocess.run([str(kicad_py), "-B", str(sync_script), "--board", str(board_path), "--apply"], **NO_WINDOW)
        return done.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
