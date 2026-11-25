#!/usr/bin/env python3
"""Produce the Gerber + drill archive that JLCPCB fabricates from.

This step was missing from the recorded pipeline: BOM, positions and designators
were scripted, but the fabrication archive was made by hand, which means the one
artefact the board is actually built from was the least reproducible thing in the
process.

The copper layers are the board's own, read from its saved file: two, four or
more. The layer set and the extensions are the ones `kicad_finalize_jlc_outputs.py`
verifies and `kicad_package_production.py` exports again to compare, both through
fabrication_layers() and fabrication_extensions() here, so the three cannot drift:
Protel extensions, plated and non-plated drills in separate files, and a job file.
It also writes the IPC-D-356 test netlist, `netlist.ipc`, beside the archive: the
packager re-exports it from the board and compares the two line by line, so the
one bundle file that can be checked by content is.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from kicad_sexp import copper_layers, parse  # noqa: E402

KICAD_CLI = pathlib.Path(__file__).resolve().parents[1] / 'tools/kicad/invoke_kicad_cli.py'

# Every layer the archive holds besides copper, by KiCad's own names, which kicad-cli takes even when a project
# has renamed a layer (e.g., Edge.Cuts renamed in a legacy library import). Order matters only for readability.
OTHER_LAYERS = [
    "F.Silkscreen", "B.Silkscreen",
    "F.Mask", "B.Mask",
    "F.Paste", "B.Paste",
    "Edge.Cuts",
]
OTHER_EXTENSIONS = {".gto", ".gbo", ".gts", ".gbs", ".gtp", ".gbp", ".gm1", ".gbrjob"}
OPTIONAL_USER_LAYERS: dict[str, tuple[tuple[str, ...], str]] = {
    "Cmts.User": (("Cmts.User", "User.Comments"), "-user_comments.gbr"),
}
_BOARD_HEADER_TAGS = frozenset({
    "version", "generator", "generator_version", "general",
    "paper", "title_block", "layers", "setup", "net",
})


def _node_is_hidden(node: list) -> bool:
    """True for hidden footprint properties so KiCad defaults on Cmts.User do not trigger an extra Gerber."""
    if not node or node[0] != "property":
        return False
    for item in node[1:]:
        if item == "hide":
            return True
        if isinstance(item, list) and item:
            if item[0] == "hide" and (len(item) == 1 or item[1] == "yes"):
                return True
            if item[0] == "effects" and any(
                sub == "hide" or (isinstance(sub, list) and sub and sub[0] == "hide" and (len(sub) == 1 or sub[1] == "yes"))
                for sub in item[1:]
            ):
                return True
    return False


def _collect_used_layers(node: object, out: set[str]) -> None:
    if not isinstance(node, list) or not node or _node_is_hidden(node):
        return
    head = node[0]
    if head == "layer" and len(node) >= 2 and isinstance(node[1], str):
        out.add(node[1])
        return
    if head == "layers":
        for item in node[1:]:
            if isinstance(item, str):
                out.add(item)
        return
    for item in node[1:]:
        if isinstance(item, list):
            _collect_used_layers(item, out)


def board_copper(board: pathlib.Path) -> list[str]:
    """The saved board's copper layers in stack order. Refuses a board without a front and a back copper layer."""
    copper = copper_layers(parse(pathlib.Path(board).read_text(encoding="utf-8")))
    if len(copper) < 2 or copper[0] != "F.Cu" or copper[-1] != "B.Cu":
        raise RuntimeError(f"{board} lists copper layers {copper or 'none'}: a front and a back layer are needed")
    return copper


def board_extra_layers(board: pathlib.Path) -> list[str]:
    """Optional fabrication drawing/comment layers populated on the saved board (beyond standard copper/mask/silk/paste/Edge.Cuts)."""
    parsed = parse(pathlib.Path(board).read_text(encoding="utf-8"))
    used: set[str] = set()
    for item in parsed[1:]:
        if isinstance(item, list) and item and item[0] not in _BOARD_HEADER_TAGS:
            _collect_used_layers(item, used)
    return [
        layer
        for layer, (aliases, _suffix) in OPTIONAL_USER_LAYERS.items()
        if any(alias in used for alias in aliases)
    ]


def fabrication_layers(copper: list[str], extra: list[str] | tuple[str, ...] = ()) -> list[str]:
    """The layers of a fabrication archive for a board with these copper layers: the copper in stack order, then
    silk, mask, paste, the outline, and any populated optional fabrication layers."""
    return [*copper, *OTHER_LAYERS, *extra]


def fabrication_extensions(copper: list[str]) -> set[str]:
    """The Gerber extensions that archive must hold. KiCad's Protel names give F.Cu .gtl, B.Cu .gbl and In<n>.Cu
    .g<n> (kicad-cli 10.0 on boards of two, four and six copper layers)."""
    return {".gtl", ".gbl", *(f".g{name[2:-3]}" for name in copper[1:-1])} | OTHER_EXTENSIONS


def fabrication_member_suffixes(extra: list[str] | tuple[str, ...] = ()) -> set[str]:
    """Lowercase member filename suffixes required when optional fabrication layers are exported."""
    return {OPTIONAL_USER_LAYERS[layer][1] for layer in extra}


def run(cmd: list[str]) -> None:
    result = subprocess.run([sys.executable, '-X', 'utf8', *cmd], capture_output=True,
                            text=True, creationflags=0x08000000 if sys.platform == 'win32' else 0)
    if result.returncode != 0:
        raise SystemExit(f"{cmd[1:3]} failed:\n{result.stdout}\n{result.stderr}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--board", type=pathlib.Path, required=True)
    parser.add_argument("--production", type=pathlib.Path, required=True)
    parser.add_argument("--base-name", required=True)
    args = parser.parse_args()

    if not KICAD_CLI.is_file():
        raise SystemExit(f"kicad-cli not found at {KICAD_CLI}")
    board = args.board.resolve()
    try:
        copper = board_copper(board)
        extra = board_extra_layers(board)
        layers = fabrication_layers(copper, extra)
    except (OSError, ValueError, RuntimeError) as error:
        raise SystemExit(f"could not read the board's copper layers: {error}")
    out_zip = (args.production / f"{args.base_name}.zip").resolve()
    args.production.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        stage = pathlib.Path(tmp)
        # Protel extensions are the default; --no-protel-ext would break the
        # extension set that kicad_finalize_jlc_outputs.py checks for.
        run([str(KICAD_CLI), "pcb", "export", "gerbers",
             "--output", str(stage) + os.sep,
             "--layers", ",".join(layers),
             "--subtract-soldermask",
             str(board)])
        run([str(KICAD_CLI), "pcb", "export", "drill",
             "--output", str(stage) + os.sep,
             "--format", "excellon",
             "--drill-origin", "absolute",
             "--excellon-separate-th",
             "--generate-map", "--map-format", "gerberx2",
             str(board)])

        produced = sorted(p for p in stage.iterdir() if p.is_file())
        if not produced:
            raise SystemExit("kicad-cli produced no fabrication files")
        with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in produced:
                archive.write(path, path.name)

    with zipfile.ZipFile(out_zip) as archive:
        names = sorted(pathlib.PurePosixPath(n).name for n in archive.namelist())
    extra_note = f"; extra layers {', '.join(extra)}" if extra else ""
    print(f"wrote {out_zip} with {len(names)} files; copper layers {', '.join(copper)}{extra_note}")
    for name in names:
        print(f"   {name}")
    netlist = (args.production / "netlist.ipc").resolve()
    run([str(KICAD_CLI), "pcb", "export", "ipcd356", "--output", str(netlist), str(board)])
    if not netlist.is_file():
        raise SystemExit("kicad-cli produced no IPC-D-356 netlist")
    print(f"wrote {netlist}")


if __name__ == "__main__":
    main()
