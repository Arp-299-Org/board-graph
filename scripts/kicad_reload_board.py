#!/usr/bin/env python3
"""Reload the open board from disk in the Mac's PCB Editor (File > Revert), after checking it has no unsaved edits.

The Mac's KiCad is the agents' own (AGENTS.md, "Two KiCads, two owners"): agents reload it whenever the file
on disk changes, without asking, and never leave unsaved edits in it. KiCad starts the editor's title with
"*" while it has unsaved edits (PCB_EDIT_FRAME::UpdateTitle), and its revert discards them without asking
(API_HANDLER_PCB::handleRevertDocument), so this title is the only guard. It refuses when any PCB Editor
title starts with "*", and unless it reads the board window's own clean title exactly once: a board whose
title it cannot read, or that is read-only or never saved, is not reloaded. With --pid, only that editor's windows count. The editor on the owner's PC is theirs,
reloaded only with their go; this script is for the Mac.

KiCad answers a revert without saying whether the file loaded (a failed load can leave the old board or an
empty one), so afterwards it compares what the editor holds with the file, item by item, and says
status=reloaded only when they match. It counts the zone fills' corners before and after, so the change it
made can be seen. Then look at the editor with scripts/mac.py look.

  pwsh -File scripts/kicad_run.ps1 -Mode Live -Project board -Script scripts/kicad_reload_board.py -ScriptArgs --project=board
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import pathlib
import shutil
import subprocess
import sys

from kipy import KiCad

import kicad_ipc
import kicad_rules_geom as rg
import kicad_sexp

# The title of every PCB Editor window, minimized ones included, as a JSON list, "" where a window has none
# (the same CoreGraphics list as scripts/mac_screen.sh). KiCad keeps several untitled helper windows, so an
# empty title is normal; the board's own window must be found by its title. Titles need Screen Recording,
# which SSH has on the Mac since 2026-10-25; without it every title is empty.
EDITOR_TITLES = '''ObjC.import("CoreGraphics");
const list = ObjC.castRefToObject($.CGWindowListCopyWindowInfo($.kCGWindowListOptionAll, $.kCGNullWindowID));
if (!list || list.isNil()) throw new Error("macOS returned no window list");
const titles = [];
for (let i = 0; i < list.count; i++) {
  const w = list.objectAtIndex(i);
  if (ObjC.unwrap(w.objectForKey("kCGWindowLayer")) !== 0) continue;
  if (ObjC.unwrap(w.objectForKey("kCGWindowOwnerName")) !== "PCB Editor") continue;
  if (ONLY_PID && ObjC.unwrap(w.objectForKey("kCGWindowOwnerPID")) !== ONLY_PID) continue;
  titles.push(ObjC.unwrap(w.objectForKey("kCGWindowName")) || "");
}
JSON.stringify(titles);'''


project_identity = kicad_ipc.project_identity


def _linux_editor_titles(pid: int) -> list[str]:
    if not os.environ.get("DISPLAY"):
        raise RuntimeError("cannot read the PCB Editor's title, so cannot tell whether it has unsaved edits: DISPLAY is not set")
    if not shutil.which("xdotool"):
        raise RuntimeError("cannot read the PCB Editor's title, so cannot tell whether it has unsaved edits: xdotool is not installed")
    search_cmd = (
        ["xdotool", "search", "--onlyvisible", "--pid", str(pid)]
        if pid
        else ["xdotool", "search", "--onlyvisible", "--name", "PCB Editor"]
    )
    done = subprocess.run(search_cmd, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        if done.returncode == 1 and not (done.stderr or "").strip() and not (done.stdout or "").strip():
            return []
        raise RuntimeError(f"cannot read the PCB Editor's title, so cannot tell whether it has unsaved edits: "
                           f"{(done.stderr or done.stdout).strip()}")
    titles = []
    for wid in (line.strip() for line in done.stdout.splitlines() if line.strip()):
        name = subprocess.run(["xdotool", "getwindowname", wid], capture_output=True, text=True, check=False)
        titles.append((name.stdout or "").strip() if name.returncode == 0 else "")
    return titles


def editor_titles(pid: int) -> list[str]:
    if sys.platform.startswith("linux"):
        return _linux_editor_titles(pid)
    done = subprocess.run(["osascript", "-l", "JavaScript", "-e", EDITOR_TITLES.replace("ONLY_PID", str(pid))],
                          capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise RuntimeError(f"cannot read the PCB Editor's title, so cannot tell whether it has unsaved edits: "
                           f"{(done.stderr or done.stdout).strip()}")
    try:
        titles = json.loads(done.stdout)
    except ValueError:
        titles = None
    if not isinstance(titles, list) or not all(isinstance(title, str) for title in titles):
        raise RuntimeError(f"cannot read the PCB Editor's title: macOS's window list came back as {done.stdout.strip()!r}")
    return titles


def fill_corners(board) -> int:
    """Corners of every zone's filled polygons, holes included: what the editor holds in memory."""
    total = 0
    for zone in board.get_zones():
        for polygons in zone.filled_polygons.values():
            for polygon in polygons:
                total += len(polygon.outline.nodes) + sum(len(hole.nodes) for hole in polygon.holes)
    return total


def snapshot(board) -> str:
    return (f"fill_corners={fill_corners(board)} tracks={len(board.get_tracks())} vias={len(board.get_vias())} "
            f"footprints={len(board.get_footprints())}")


frozen = rg.frozen_sexp


def board_items(text: str) -> collections.Counter:
    """The board's top-level items in KiCad's file format, order and harmless KiCad 8->10 normalizations ignored."""
    root = kicad_sexp.parse(text)
    if not isinstance(root, list) or not root or root[0] != "kicad_pcb":
        raise ValueError("not a KiCad board")
    net_map = rg.board_net_map(root)
    items = collections.Counter()
    for item in root[1:]:
        if rg.skip_guard_top_level(item):
            continue
        items[rg.guard_item_frozen(item, net_map)] += 1
    return items


def shown(item) -> str:
    text = repr(item)
    return text if len(text) <= 300 else text[:300] + "..."


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=pathlib.Path, required=True)
    parser.add_argument("--pid", type=int, default=0,
                        help="only this editor's windows count (its socket comes in KICAD_API_SOCKET), so other "
                             "sessions' editors, even of a board of the same name, are left out")
    args = parser.parse_args()

    if sys.platform not in ("darwin", "linux"):
        print("status=refused reason=this reloads the KiCad machine's editor only; on the owner's PC the editor is theirs, "
              "reloaded only with their go in that turn (AGENTS.md)")
        return 2
    expected_directory, expected_board = project_identity(args.project)
    kicad = KiCad(timeout_ms=60_000)
    board = kicad.get_board()
    live_project = pathlib.Path(str(board.document.project.path)).resolve()
    live_directory = live_project.parent if live_project.suffix == ".kicad_pro" else live_project
    if live_directory != expected_directory or board.document.board_filename != expected_board:
        print(f"status=refused reason=wrong live project: project={board.document.project.path} "
              f"board={board.document.board_filename}")
        return 2
    board_file = expected_directory / expected_board
    try:
        on_disk = board_file.read_bytes()
        expected_items = board_items(on_disk.decode("utf-8"))
    except (OSError, ValueError) as error:
        print(f"status=refused reason=cannot read the board file {board_file}: {error}")
        return 2
    before = snapshot(board)

    # Read the titles last, right before the reload, so the gap in which an edit could slip in stays short.
    try:
        windows = editor_titles(args.pid)
    except RuntimeError as error:
        print(f"status=refused reason={error}")
        return 2
    titles = [title for title in windows if title]
    unsaved = [title for title in titles if title.startswith("*")]
    if unsaved:
        print(f"status=refused reason=unsaved edits in the editor, which a reload would discard: {unsaved}. "
              "Agents never leave unsaved edits in the Mac's KiCad: find whose they are, and save or drop them first.")
        return 2
    # What UpdateTitle writes for a saved, writable board without unsaved edits.
    stem = pathlib.Path(expected_board).stem
    board_title = f"{stem} {chr(0x2014)} PCB Editor"
    accepted_titles = (board_title, f"{stem} - PCB Editor")
    matched_titles = [title for title in titles if title in accepted_titles]
    if len(matched_titles) != 1:
        print(f"status=refused reason=the board's window was not found by its title exactly once, so its unsaved "
              f"state is not known: looked for {board_title!r} among the readable titles {titles}; "
              f"{len(windows) - len(titles)} PCB Editor windows have no title (all of them when this session "
              "has lost Screen Recording)")
        return 2
    board_title = matched_titles[0]

    board.revert()
    print(f"title={board_title}")
    print(f"before {before}")

    # KiCad answers a revert without saying whether the file loaded (it can leave the old board or an empty
    # one), so compare what the editor now holds with the file, item by item.
    after = kicad.get_board()
    print(f"after  {snapshot(after)}")
    if board_file.read_bytes() != on_disk:
        print("status=verification_failed reason=the board file changed during the reload; reload again, then look")
        return 3
    held = board_items(after.get_as_string())
    if held != expected_items:
        only_file, only_editor = expected_items - held, held - expected_items
        print(f"status=verification_failed reason=the editor does not hold the file: {sum(only_file.values())} "
              f"items only in the file, {sum(only_editor.values())} only in the editor. Look at the editor.")
        for label, items in (("only in the file", only_file), ("only in the editor", only_editor)):
            for item in list(items)[:3]:
                print(f"  {label}: {shown(item)}")
        return 3
    print(f"status=reloaded board={board.document.board_filename} items={sum(held.values())}, the same as the file")
    return 0


if __name__ == "__main__":
    sys.exit(main())
