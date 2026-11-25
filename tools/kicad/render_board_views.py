#!/usr/bin/env python3
"""Render review views of a KiCad board: per-layer PDFs turned into PNGs, and 3D renders.

Normal Python with pymupdf on the path. KiCad CLI runs through invoke_kicad_cli.py.
On Windows the whole render runs on the KiCad host (scripts/mac.py) and the
pictures come back, so no KiCad window opens on the owner's screen.

    python tools/kicad/render_board_views.py --board X.kicad_pcb --out renders/dir

Produces one PNG per VIEWS entry (the inner-layer views on a four-layer board
only), front_no_pour.png and back_no_pour.png (zones stripped by KiCad's
Python, when it is available), 3d_top.png and 3d_bottom.png.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import shlex
import shutil
import tempfile
import uuid
import subprocess
import sys


from kicad_install import KICAD_PYTHON as KICAD_PY

TOOL_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(TOOL_ROOT / "scripts"))
import tool_paths  # noqa: E402

if __name__ == "__main__":
    tool_paths.take_argument()

ROOT = tool_paths.project_root()
from board_review_inputs import model_inputs  # noqa: E402

LAUNCHER = TOOL_ROOT / "tools" / "kicad" / "invoke_kicad_cli.py"
# Children are console programs; on Windows they would open a window over the
# user's screen unless told not to (KiCad's Python even more so: importing
# pcbnew starts its GUI toolkit).
HIDDEN = (
    {"creationflags": subprocess.CREATE_NO_WINDOW, "stdin": subprocess.DEVNULL}
    if sys.platform == "win32" else {}
)

VIEWS = {
    "front_copper": "F.Cu,F.Mask,Edge.Cuts,F.Fab,F.SilkS",
    "back_copper": "B.Cu,B.Mask,Edge.Cuts,B.SilkS",
    "front_copper_only": "F.Cu,Edge.Cuts",
    "back_copper_only": "B.Cu,Edge.Cuts",
    "front_silk_and_mask": "F.SilkS,F.Mask,F.Paste,Edge.Cuts,F.CrtYd",
    "back_silk_and_mask": "B.SilkS,B.Mask,Edge.Cuts",
    "in1_copper": "In1.Cu,Edge.Cuts",   # four-layer boards only; a two-layer board exports these as empty outlines
    "in2_copper": "In2.Cu,Edge.Cuts",
    "all_copper_stack": "F.Cu,In1.Cu,In2.Cu,B.Cu,Edge.Cuts",
}


def cli(*args: str) -> None:
    r = subprocess.run([sys.executable, str(LAUNCHER), *args], capture_output=True, text=True, **HIDDEN)
    if r.returncode != 0:
        raise SystemExit(f"kicad-cli failed: {' '.join(args)}\n{r.stdout}\n{r.stderr}")


def pdf_to_png(pdf: pathlib.Path, png: pathlib.Path, dpi: int = 900) -> None:
    import fitz  # pymupdf
    doc = fitz.open(str(pdf)); page = doc[0]
    drawings = page.get_drawings()
    if drawings:
        x0 = min(d["rect"].x0 for d in drawings); y0 = min(d["rect"].y0 for d in drawings)
        x1 = max(d["rect"].x1 for d in drawings); y1 = max(d["rect"].y1 for d in drawings)
        clip = fitz.Rect(x0 - 4, y0 - 4, x1 + 4, y1 + 4)
    else:
        clip = page.rect
    page.get_pixmap(dpi=dpi, clip=clip).save(str(png))


def strip_zones(board: pathlib.Path, out: pathlib.Path) -> bool:
    code = (
        "import pcbnew,sys\n"
        f"b=pcbnew.LoadBoard(r'{board}')\n"
        "for z in list(b.Zones()):\n"
        "    if not z.GetIsRuleArea(): b.Remove(z)\n"
        f"pcbnew.SaveBoard(r'{out}', b)\n"
    )
    r = subprocess.run([str(KICAD_PY), "-c", code], capture_output=True, text=True, **HIDDEN)
    return r.returncode == 0


def resolve_board_arg(raw: str, root: pathlib.Path = ROOT) -> pathlib.Path:
    """Resolve a `.kicad_pcb` path, a board folder containing `board.json`, or a board name."""
    candidate = pathlib.Path(raw)
    if candidate.is_file():
        return candidate.resolve()
    if not candidate.is_absolute() and (root / candidate).is_file():
        return (root / candidate).resolve()
    import board_description
    import boards
    for folder in (candidate, *( () if candidate.is_absolute() else (root / candidate,) )):
        desc = folder / "board.json"
        if folder.is_dir() and desc.is_file():
            return board_description.load(desc, root=root).path("pcb").resolve()
    try:
        found = boards.find(raw, root=root)
    except boards.BoardsError:
        return candidate.resolve()
    return board_description.load(found.description, root=root).path("pcb").resolve()


def render_on_mac(board: pathlib.Path, out: pathlib.Path) -> None:
    """On Windows the no-pour views would need KiCad's Python, and importing pcbnew opens a window over the
    user's screen. So the whole render runs on the KiCad host, the agents' machine, from a copy of the board,
    its project file, and its local models folder in a temporary source folder, and the pictures come back here."""
    import base64, io, tarfile
    import mac
    tag = f"render-{uuid.uuid4().hex}"
    remote_src, remote_out = f"/tmp/board-graph-src-{tag}", f"/tmp/board-graph-{tag}"
    remote_board = f"{remote_src}/{shlex.quote(board.name)}"
    bundle = io.BytesIO()
    with tarfile.open(fileobj=bundle, mode="w:gz") as tar:
        tar.add(board, arcname=board.name)
        project = board.with_suffix(".kicad_pro")
        if project.is_file():
            tar.add(project, arcname=project.name)
        models_dir = board.parent / "models"
        if models_dir.is_dir():
            for model in sorted(models_dir.rglob("*")):
                if model.is_file():
                    tar.add(model, arcname=model.relative_to(board.parent).as_posix())
    payload = base64.b64encode(bundle.getvalue()).decode("ascii")
    unpack = (
        f"mkdir -p {remote_src} && "
        "python3 -c 'import sys,base64; sys.stdout.buffer.write(base64.b64decode(sys.stdin.read()))' "
        f"| tar -C {remote_src} -xzf -"
    )
    if mac.ssh(unpack, payload).returncode:
        raise SystemExit("could not copy the board and models to the Mac")
    env_prefix = 'BOARD_GRAPH_PROJECT="$PWD" ' if mac.TOOL_CHECKOUT else ""
    remote_script = mac.tool_script("tools/kicad/render_board_views.py")
    result = mac.ssh("bash -s", mac.PATH + f"""cd {mac.CHECKOUT}
{env_prefix}python3 {remote_script} --board {remote_board} --out {remote_out} >&2; status=$?
rm -rf {remote_src}
[ $status = 0 ] && tar -C {remote_out} -cf - .
rm -rf {remote_out}
exit $status
""", timeout=900)
    if result.returncode:
        raise SystemExit("render on the Mac failed:\n" + mac.text(result.stderr)[-2000:])
    with tarfile.open(fileobj=io.BytesIO(result.stdout)) as pictures:
        pictures.extractall(out)
    for png in sorted(out.glob("*.png")):
        print("wrote", png)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--board", type=str, required=True)
    ap.add_argument("--out", type=pathlib.Path, required=True)
    args = ap.parse_args()
    board = resolve_board_arg(args.board)
    if not board.is_file():
        raise SystemExit(f"board file not found: {board}")
    out = args.out.resolve(); out.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        render_on_mac(board, out)
        return
    render_snapshot(board, out)


def render_snapshot(board, out, root=None):
    # Keep the source beside its models, with a private filename throughout this render.
    with tempfile.TemporaryDirectory(prefix="board-views-") as folder:
        stage = pathlib.Path(folder)
        saved = board.with_name(f".review-{uuid.uuid4().hex}.kicad_pcb")
        project = saved.with_suffix(".kicad_pro")
        try:
            content = board.read_bytes()
            model_dir = stage / "models"
            model_dir.mkdir()
            project_root = root or (ROOT if board.is_relative_to(ROOT) else board.parent)
            models, frozen = model_inputs(content.decode("utf-8"), board.parent, project_root, freeze=model_dir)
            saved.write_bytes(frozen.encode("utf-8"))
            if board.with_suffix(".kicad_pro").is_file():
                shutil.copyfile(board.with_suffix(".kicad_pro"), project)
            render_views(saved, stage)
            images = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(stage.glob("*.png"))}
            if not images:
                raise ValueError("The board render produced no images")
            receipt = {"schema": "board-graph/board-render/v2", "models": models,
                       "board_sha256": hashlib.sha256(content).hexdigest(), "images": images}
            missing = [ref for ref, value in models.items() if value is None]
            if missing:
                print("Render cannot establish placement approval. Unverified models: " + ", ".join(missing), file=sys.stderr)
            for name in images:
                shutil.copyfile(stage / name, out / name)
            (out / "board-render.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        finally:
            for path in (saved, project, saved.with_suffix(".kicad_prl")):
                path.unlink(missing_ok=True)


def render_views(board, out):
    four_layer = '"In1.Cu"' in board.read_text(encoding="utf-8")
    for name, layers in VIEWS.items():
        if not four_layer:
            if name.startswith("in"):
                continue
            if name == "all_copper_stack":
                layers = "F.Cu,B.Cu,Edge.Cuts"
        pdf = out / f"{name}.pdf"
        cli("pcb", "export", "pdf", "--layers", layers, "--mode-single", "--output", str(pdf), str(board))
        pdf_to_png(pdf, out / f"{name}.png"); pdf.unlink()
        print("wrote", out / f"{name}.png")
    nz = out / "_no_zones.kicad_pcb"
    if strip_zones(board, nz):
        for name, layers in (("front_no_pour", "F.Cu,Edge.Cuts,F.Fab"), ("back_no_pour", "B.Cu,Edge.Cuts")):
            pdf = out / f"{name}.pdf"
            cli("pcb", "export", "pdf", "--layers", layers, "--mode-single", "--output", str(pdf), str(nz))
            pdf_to_png(pdf, out / f"{name}.png"); pdf.unlink()
            print("wrote", out / f"{name}.png")
        nz.unlink()
        for stray in (nz.with_suffix('.kicad_pro'), nz.with_suffix('.kicad_prl')):
            if stray.exists():
                stray.unlink()
    for side in ("top", "bottom"):
        png = out / f"3d_{side}.png"
        cli("pcb", "render", "--side", side, "--zoom", "0.9", "--width", "1600", "--height", "1600", "--quality", "high", "--output", str(png), str(board))
        print("wrote", png)


if __name__ == "__main__":
    main()
