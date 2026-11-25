#!/usr/bin/env python3
"""Hover board positions in the open PCB Editor on the Mac and press a key at each, with the route tool's own
window mapping and its status-bar proof of the pointer position: `E` opens an item's properties, `Backspace`
deletes it. Runs on the Mac with the kipy venv (`.venv-kicad-ipc/bin/python`); nothing is clicked or sent when
the status bar disagrees with the aim.

  kicad_hover_key.py <pid> '<json steps>' [scale]   steps: [[x_mm, y_mm, key_or_null], ...]; key is a mac.py key
                                                    name (backspace, return, esc...) or one character (e); null
                                                    hovers only. scale: points per mm to zoom to (default 45,
                                                    at most the route tool's MAX_SCALE)
  kicad_hover_key.py <pid> --zones                  the board's zones and rule areas, over IPC

Each step prints one JSON line: the aim, what the status bar read there, and the key sent. When two items share
the spot, KiCad lists them with their lengths: answer with `mac.py keys <pid> <number>`. Inside a via's ring the
hover picks the via before a track: untick Vias in the Selection Filter first. Pressing a key with an item
selected acts on the selection, so the run clears the selection before it starts.
"""
import json
import math
import pathlib
import sys
import time
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import kicad_route as kr  # noqa: E402
import mac  # noqa: E402


def main(argv: list[str]) -> int:
    pid = argv[0]
    window = kr.pick_editor(pid)
    editor, board = kr.MacEditor(window), kr.KipyBoard(pid)
    if argv[1] == "--zones":
        def val(v):
            return v() if callable(v) else v
        for zone in board.board.get_zones():
            print(json.dumps({"name": val(zone.name), "layers": [str(l) for l in val(zone.layers)],
                              "rule_area": bool(val(zone.is_rule_area))}))
        return 0
    steps = json.loads(argv[1])
    want = min(float(argv[2]) if len(argv) > 2 else 45.0, kr.MAX_SCALE)
    points = [(s[0], s[1]) for s in steps]
    run = kr.Run(editor, board, kr.Request("", "", []))
    editor.activate()
    board.clear_selection()
    editor.forget_view()  # always calibrate afresh: a panel click or a pan leaves the cached view stale
    editor.read_at(*editor.safe_rect()[:2])  # the pointer on the canvas first, so the status bar follows it
    pads = [types.SimpleNamespace(half=0.07)]
    run.frame(points, pads)
    safe = editor.safe_rect()
    middle = (sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points))
    for _ in range(6):
        if run.mapping.scale >= want * 0.9:
            break
        steps_in = max(1, min(kr.ZOOM_MOST, int(math.floor(math.log(want / run.mapping.scale) / math.log(kr.ZOOM_STEP) + 0.5))))
        editor.scroll(*run.mapping.to_screen(*middle), steps_in)
        editor.wait(0.4)
        run.mapping = run.calibrate()
        if not kr.fits(run.mapping, points, safe, 0.07, closely=False):
            raise kr.RouteFailure("mapping_failed", f"after zooming to {run.mapping.scale:.1f} points per mm the points no longer all fit")
    print(json.dumps({"scale": round(run.mapping.scale, 2), "error": round(run.mapping.error, 4), "grid": run.grid}))
    for x, y, key in steps:
        sx, sy = run.mapping.to_screen(x, y)
        reading = run.hover(x, y, sx, sy, f"({x}, {y})", max(run.grid / 2 + run.mapping.error, 0.03))
        if key:
            if editor._run("top", guarded=False).strip() != editor.number:
                raise kr.RouteFailure("lost_focus", "the editor window is not on top: nothing was sent")
            mac.osascript('tell application "System Events" to ' + mac.key_line(key))
            time.sleep(0.5)
        print(json.dumps({"aim": [x, y], "status": [reading.x, reading.y], "zoom": reading.zoom, "key": key}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except kr.RouteFailure as failure:
        print(json.dumps({"ok": False, "status": failure.status, "reason": failure.reason}))
        sys.exit(1)
