#!/usr/bin/env python3
"""Keep the project footprint library identical to the board, and holding nothing else.

Each footprint the board takes from the project library is compared with its library copy the
way DRC does, and by its 3D models; with --apply, a copy that differs is replaced by the board's footprint, saved the way
KiCad's own export saves it (reference REF**, at the origin, unrotated, no nets), and a library
footprint no board part uses is deleted. Offline: reads a saved board, writes only .kicad_mod files.
The board and the library's name come from the board's description (scripts/board_description.py); the
library is the folder of that name beside the board.

  kicad_run.ps1 -Mode Offline -Project <board folder> -Script scripts/kicad_footprints_to_library.py -ScriptArgs --apply
"""
from __future__ import annotations

import argparse
import pathlib

import pcbnew

import board_description

DESCRIPTION = board_description.load()
LIBRARY = DESCRIPTION.library  # the library's nickname, as the footprint table and every board footprint name it
LIBRARY_FOLDER = pathlib.PurePosixPath(DESCRIPTION.rel("footprint_library")).name


def plugin() -> pcbnew.PCB_IO_KICAD_SEXPR:
    found = pcbnew.PCB_IO_MGR.FindPlugin(pcbnew.PCB_IO_MGR.KICAD_SEXP)
    if found is None:
        raise RuntimeError("KiCad s-expression plugin is unavailable")
    return found


# FOOTPRINT::COMPARE_FLAGS::DRC, the comparison DRC's library-mismatch test makes
DRC_COMPARE = getattr(pcbnew.FOOTPRINT, "DRC", getattr(pcbnew, "FOOTPRINT_DRC", 1))


def models(fp) -> list:
    """The 3D models, which DRC's comparison leaves out: file, offset, rotation, scale."""
    def v(vec):
        return tuple(round(c, 4) for c in (vec.x, vec.y, vec.z))
    return [(m.m_Filename, v(m.m_Offset), v(m.m_Rotation), v(m.m_Scale)) for m in fp.Models()]


def differs(board_fp, library_fp) -> bool:
    return library_fp is None or board_fp.FootprintNeedsUpdate(library_fp, DRC_COMPARE)


def source_of(name: str, fps: list):
    """The part a shared footprint is written from: the one EasyEDA named it after, else the first.
    Its 3D model goes into the library; other parts on the footprint keep their own model."""
    return next((fp for fp in fps if name.endswith("__" + fp.GetReference())), fps[0])


def stale(name: str, fps: list, library_fp) -> list:
    source = source_of(name, fps)
    different = [fp for fp in fps if differs(fp, library_fp)]
    if library_fp is not None and models(source) != models(library_fp) and source not in different:
        different.append(source)
    return different


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board", type=pathlib.Path, default=DESCRIPTION.path("pcb"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--explain", metavar="REF", help="print why one part's footprint differs from its library copy")
    args = parser.parse_args()
    board = pcbnew.LoadBoard(str(args.board.resolve()))
    library = args.board.resolve().parent / LIBRARY_FOLDER
    io = plugin()
    if args.explain:
        fp = board.FindFootprintByReference(args.explain)
        reporter = next((getattr(pcbnew, n).GetInstance() for n in ("STDOUT_REPORTER", "WXLOG_REPORTER")
                         if hasattr(getattr(pcbnew, n, None), "GetInstance")), None)
        library_fp = io.FootprintLoad(str(library), str(fp.GetFPID().GetLibItemName()), False)
        print("reporter:", type(reporter).__name__)
        print("differs:", fp.FootprintNeedsUpdate(library_fp, DRC_COMPARE, reporter))
        return

    used: dict[str, list] = {}
    for fp in board.GetFootprints():
        fpid = fp.GetFPID()
        if str(fpid.GetLibNickname()) == LIBRARY:
            used.setdefault(str(fpid.GetLibItemName()), []).append(fp)

    todo = []
    for name, fps in sorted(used.items()):
        different = stale(name, fps, io.FootprintLoad(str(library), name, False))
        if different:
            source = source_of(name, different)
            todo.append((name, source, [fp.GetReference() for fp in different]))
    unused = sorted(p for p in library.glob("*.kicad_mod") if p.stem not in used)

    for name, source, refs in todo:
        print(f"differs: {name} ({', '.join(refs)}); board copy from {source.GetReference()}")
    for path in unused:
        print(f"unused: {path.stem}")
    if not args.apply:
        print(f"{len(todo)} to write back, {len(unused)} unused; dry run")
        return

    for name, source, _ in todo:
        copy = pcbnew.FOOTPRINT(source)
        copy.SetParent(None)
        copy.SetReference("REF**")
        io.FootprintSave(str(library), copy)
    for path in unused:
        path.unlink()
    left = [name for name, fps in sorted(used.items())
            if stale(name, fps, io.FootprintLoad(str(library), name, False))]
    print(f"wrote back {len(todo)}, deleted {len(unused)}; still differing: {left or 'none'}")
    if left:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
