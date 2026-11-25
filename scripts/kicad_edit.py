#!/usr/bin/env python3
"""Exact edits in the open PCB Editor on the Mac, over KiCad's IPC: the router finds paths, this makes exact changes.

  .venv-kicad-ipc/bin/python scripts/kicad_edit.py list --net /ENC_A [--box X0 Y0 X1 Y1]
  .venv-kicad-ipc/bin/python scripts/kicad_edit.py list --texts [--box X0 Y0 X1 Y1]
  .venv-kicad-ipc/bin/python scripts/kicad_edit.py delete ID [ID ...]
  .venv-kicad-ipc/bin/python scripts/kicad_edit.py move-via ID X Y
  .venv-kicad-ipc/bin/python scripts/kicad_edit.py move-text ID X Y
  .venv-kicad-ipc/bin/python scripts/kicad_edit.py move-text --ref R44 X Y

Positions are board millimetres. With several PCB Editors open, name one with --pid (from `mac.py windows`).

`list` reads the editor's board as it is, unsaved edits included. `delete` takes tracks, arcs and vias only, and
does nothing unless every ID is one of them. `move-via` moves the via and every track end stored at exactly its
point, on its net and on a layer it spans; it refuses when an arc ends there or a track of its net ends inside it
off its centre, since those would be left behind. `move-text` moves a board text, or with --ref a footprint's
reference field; the footprint itself stays where it is.

Each edit is one commit, so one undo in the editor takes it back. Afterwards the tool reads the board again and
compares: the edited items must be gone or at the new point, every other track, arc and via as it was, and after
a text move every other text, and each footprint's position, side, 3D models and item count, too.
It never saves, reverts, creates copper or moves a footprint. It prints one JSON result and exits 0 only when `ok`.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import math
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import mac  # noqa: E402

NM = 1_000_000  # nanometres in a millimetre


class EditFailure(Exception):
    """The call stopped. `status` is a word a caller can branch on, and `reason` says what happened."""

    def __init__(self, status: str, reason: str):
        super().__init__(reason)
        self.status, self.reason = status, reason


def to_nm(mm: float) -> int:
    """Round, never truncate: kicad-python's from_mm made 134.2 mm into 134199999 nm."""
    return int(round(mm * NM))


def to_mm(nm: int) -> float:
    return round(nm / NM, 6)


@dataclasses.dataclass(frozen=True)
class Copper:
    """A track, arc or via in stored integer nanometres. A via's start and end are its position."""
    id: str
    kind: str  # "track", "arc" or "via"
    net: str
    layers: tuple  # a track's one layer, or every copper layer a via spans
    start: tuple
    end: tuple
    width: int  # a via's diameter
    locked: bool = False
    mid: tuple | None = None

    def view(self) -> dict:
        out = {"id": self.id, "kind": self.kind, "net": self.net}
        if self.kind == "via":
            out.update(layers=list(self.layers), at=[to_mm(c) for c in self.start], diameter=to_mm(self.width))
        else:
            out.update(layer=self.layers[0], start=[to_mm(c) for c in self.start], end=[to_mm(c) for c in self.end],
                       width=to_mm(self.width))
            if self.mid is not None:
                out["mid"] = [to_mm(c) for c in self.mid]
        if self.locked:
            out["locked"] = True
        return out


@dataclasses.dataclass(frozen=True)
class Text:
    """A board text, or a footprint's reference field (`ref` set, `id` the footprint's). `footprint` is the
    footprint's position, orientation and side, which a move of its reference must leave alone."""
    id: str
    kind: str  # "text" or "reference"
    text: str
    layer: str
    at: tuple
    locked: bool = False
    ref: str | None = None
    footprint: tuple | None = None

    def view(self) -> dict:
        out = {"id": self.id, "kind": self.kind, "text": self.text, "layer": self.layer, "at": [to_mm(c) for c in self.at]}
        if self.ref:
            out["ref"] = self.ref
        if self.locked:
            out["locked"] = True
        return out


# What to do, against any board: on the Mac the KiCad class below, in the tests a stand-in.

def in_box(box, *points) -> bool:
    """Whether any of these points, or the segment between the first two, is in the box (mm)."""
    x0, y0, x1, y1 = (to_nm(v) for v in (min(box[0], box[2]), min(box[1], box[3]), max(box[0], box[2]), max(box[1], box[3])))
    if any(x0 <= p[0] <= x1 and y0 <= p[1] <= y1 for p in points):
        return True
    if len(points) < 2:
        return False
    (ax, ay), (bx, by) = points[0], points[1]
    low, high = 0.0, 1.0  # Liang-Barsky: clip the segment to the box
    for p, q in ((-(bx - ax), ax - x0), (bx - ax, x1 - ax), (-(by - ay), ay - y0), (by - ay, y1 - ay)):
        if p == 0:
            if q < 0:
                return False
            continue
        t = q / p
        if p < 0:
            low = max(low, t)
        else:
            high = min(high, t)
        if low > high:
            return False
    return True


def followers(via: Copper, copper: list) -> tuple:
    """The track ends that move with this via: same net, stored at exactly its point, on a layer it spans. And
    what would be left behind: an arc ending there, or a track end of its net inside the via but off its centre."""
    moving, stuck = [], []
    reach = via.width / 2
    for item in copper:
        if item.kind == "via" or item.net != via.net or item.layers[0] not in via.layers:
            continue
        ends = [name for name, point in (("start", item.start), ("end", item.end)) if point == via.start]
        if ends and item.kind == "arc":
            stuck.append(f"arc {item.id} ends on the via: moving one end would bend the arc")
        elif ends:
            moving.append((item, tuple(ends)))
        for name, point in (("start", item.start), ("end", item.end)):
            if point != via.start and math.dist(point, via.start) <= reach:
                stuck.append(f"{item.kind} {item.id} has its {name} inside the via, "
                             f"{to_mm(round(math.dist(point, via.start)))} mm off its centre")
    return moving, stuck


def moved_end(item: Copper, ends: tuple, point: tuple) -> Copper:
    return dataclasses.replace(item, **{name: point for name in ends})


def compare(before: list, after: list, expect: dict) -> list:
    """Every way the board read back differs from what this edit should have left: `expect` maps an id to the
    item it should now be, or None for gone. Everything else must be exactly as it was."""
    was, now = {i.id: i for i in before}, {i.id: i for i in after}
    problems = []
    for key, item in was.items():
        want = expect.get(key, item)
        got = now.get(key)
        if want is None and got is not None:
            problems.append(f"{item.kind} {key} is still on the board")
        elif want is not None and got is None:
            problems.append(f"{item.kind} {key} is gone")
        elif want is not None and got != want:
            problems.append(f"{item.kind} {key} reads back as {got.view()}, not {want.view()}")
    for key in now:
        if key not in was:
            problems.append(f"a new {now[key].kind} {key} appeared")
    return problems


class Edit:
    def __init__(self, board):
        self.board = board

    def result(self, verb: str, **extra) -> dict:
        return {"ok": True, "verb": verb, "status": "done", "board": self.board.file(), **extra}

    def list_net(self, net: str, box) -> dict:
        if net not in self.board.nets():
            raise EditFailure("unknown_net", f"no net {net!r} on this board")
        items = [i for i in self.board.copper() if i.net == net and (box is None or in_box(box, i.start, i.end))]
        return self.result("list", net=net, items=[i.view() for i in items])

    def list_texts(self, box) -> dict:
        texts = [t for t in self.board.texts() if box is None or in_box(box, t.at)]
        return self.result("list", items=[t.view() for t in texts])

    def delete(self, ids: list) -> dict:
        before = self.board.copper()
        found = {i.id: i for i in before}
        missing = [key for key in ids if key not in found]
        if missing:
            raise EditFailure("not_found", f"not a track, arc or via on this board: {', '.join(missing)}. Nothing was deleted")
        locked = [key for key in ids if found[key].locked]
        if locked:
            raise EditFailure("locked", f"locked: {', '.join(locked)}. Nothing was deleted")
        targets = [found[key] for key in dict.fromkeys(ids)]
        self.board.delete([t.id for t in targets], f"Delete {len(targets)} track(s) or via(s)")
        problems = compare(before, self.board.copper(), {t.id: None for t in targets})
        return self.checked("delete", problems, items=[{"id": t.id, "kind": t.kind, "before": t.view(), "after": None} for t in targets])

    def move_via(self, key: str, x: float, y: float) -> dict:
        before = self.board.copper()
        via = next((i for i in before if i.id == key), None)
        if via is None or via.kind != "via":
            raise EditFailure("not_found", f"{key} is not a via on this board")
        point = (to_nm(x), to_nm(y))
        if point == via.start:
            raise EditFailure("bad_request", "the via is already there")
        moving, stuck = followers(via, before)
        if stuck:
            raise EditFailure("would_leave_copper", "; ".join(stuck) + ". Nothing was moved")
        locked = [i.id for i in [via] + [m[0] for m in moving] if i.locked]
        if locked:
            raise EditFailure("locked", f"locked: {', '.join(locked)}. Nothing was moved")
        expect = {via.id: dataclasses.replace(via, start=point, end=point)}
        expect.update({item.id: moved_end(item, ends, point) for item, ends in moving})
        self.board.move_via(via.id, point, [(item.id, ends) for item, ends in moving],
                            f"Move via to ({to_mm(point[0])}, {to_mm(point[1])}) with {len(moving)} track end(s)")
        problems = compare(before, self.board.copper(), expect)
        items = [{"id": i, "kind": expect[i].kind, "before": next(b for b in before if b.id == i).view(), "after": expect[i].view()}
                 for i in expect]
        return self.checked("move-via", problems, items=items, track_ends_moved=sum(len(e) for _, e in moving))

    def move_text(self, key: str | None, ref: str | None, x: float, y: float) -> dict:
        before = self.board.texts()
        if ref:
            hits = [t for t in before if t.kind == "reference" and t.ref == ref]
            what = f"no footprint {ref}" if not hits else f"{len(hits)} footprints {ref}"
        else:
            hits = [t for t in before if t.id == key and t.kind == "text"]
            what = f"{key} is not a board text" + (": for a reference field use --ref" if any(t.id == key for t in before) else "")
        if len(hits) != 1:
            raise EditFailure("not_found", what)
        text = hits[0]
        if text.locked:
            raise EditFailure("locked", f"{text.id} is locked. Nothing was moved")
        point = (to_nm(x), to_nm(y))
        if point == text.at:
            raise EditFailure("bad_request", "the text is already there")
        copper = self.board.copper()
        self.board.move_text(text, point, f"Move {'reference ' + ref if ref else 'text'} to ({to_mm(point[0])}, {to_mm(point[1])})")
        want = dataclasses.replace(text, at=point)
        problems = compare(before, self.board.texts(), {text.id: want})
        problems += compare(copper, self.board.copper(), {})
        return self.checked("move-text", problems, items=[{"id": text.id, "kind": text.kind, "before": text.view(), "after": want.view()}])

    def checked(self, verb: str, problems: list, **extra) -> dict:
        out = self.result(verb, **extra)
        if problems:
            out.update(ok=False, status="read_back_differs", reason="; ".join(problems[:10]),
                       note="the commit was pushed: one undo in the editor takes it back")
        return out


# The Mac: the open editor over kicad-python.

class KipyEditBoard:
    """The open board, through the route tool's connection: its own socket, and a wait while KiCad is busy."""

    def __init__(self, pid: str):
        import kicad_route as kr  # mac.py, kicad_route_quality and, inside, kipy: none needed by the tests
        from kipy.geometry import Vector2
        from kipy.util.board_layer import canonical_name, is_copper_layer
        self.Vector2, self.name_of, self.is_copper = Vector2, canonical_name, is_copper_layer
        self.route = kr.KipyBoard(pid)
        self.board = self.route.board
        self.raw: dict = {}
        self.enabled = None  # the board's layers, read once

    def wait_ready(self) -> None:
        try:
            self.route.wait_ready()
        except Exception as error:  # the route tool's RouteFailure: "busy" or another IPC failure
            raise EditFailure("busy" if "busy" in str(error).lower() else "ipc_failed", str(error)) from error

    def file(self) -> str:
        return str(pathlib.PurePosixPath(self.board.document.project.path) / self.board.document.board_filename)

    def nets(self) -> set:
        self.wait_ready()
        return {net.name for net in self.board.get_nets()}

    def _via_layers(self, via) -> tuple:
        drill = via.padstack.drill
        low, high = sorted((drill.start_layer, drill.end_layer))  # kipy numbers copper F.Cu, In1.Cu ... B.Cu in order
        if self.enabled is None:
            self.enabled = set(self.board.get_enabled_layers())
        return tuple(self.name_of(layer) for layer in range(low, high + 1) if self.is_copper(layer) and layer in self.enabled)

    def copper(self) -> list:
        self.wait_ready()
        out = []
        for track in self.board.get_tracks():
            key = str(track.id.value)
            self.raw[key] = track
            arc = hasattr(track, "mid")
            out.append(Copper(key, "arc" if arc else "track", track.net.name, (self.name_of(track.layer),),
                              (track.start.x, track.start.y), (track.end.x, track.end.y), track.width, track.locked,
                              (track.mid.x, track.mid.y) if arc else None))
        for via in self.board.get_vias():
            key = str(via.id.value)
            self.raw[key] = via
            at = (via.position.x, via.position.y)
            out.append(Copper(key, "via", via.net.name, self._via_layers(via), at, at, via.diameter, via.locked))
        return out

    def texts(self) -> list:
        self.wait_ready()
        out = []
        for text in self.board.get_text():
            if not hasattr(text, "position"):
                continue  # a text box has corners, not a position: not this tool's
            key = str(text.id.value)
            self.raw[key] = text
            out.append(Text(key, "text", text.value, self.name_of(text.layer), (text.position.x, text.position.y), text.locked))
        for footprint in self.board.get_footprints():
            key = str(footprint.id.value)
            self.raw[key] = footprint
            field = footprint.reference_field
            pose = (footprint.position.x, footprint.position.y, round(footprint.orientation.degrees, 6),
                    self.name_of(footprint.layer), len(footprint.definition.models), len(footprint.definition.items))
            out.append(Text(key, "reference", field.text.value, self.name_of(field.layer),
                            (field.text.position.x, field.text.position.y), False, field.text.value, pose))
        return out

    def _commit(self, change, message: str) -> None:
        self.wait_ready()
        commit = self.board.begin_commit()
        try:
            change()
        except Exception:
            self.board.drop_commit(commit)
            raise
        self.board.push_commit(commit, message)

    def delete(self, ids: list, message: str) -> None:
        self._commit(lambda: self.board.remove_items([self.raw[key] for key in ids]), message)

    def move_via(self, key: str, point: tuple, ends: list, message: str) -> None:
        at = self.Vector2.from_xy(*point)
        via = self.raw[key]
        via.position = at
        changed = [via]
        for track_key, names in ends:
            track = self.raw[track_key]
            for name in names:
                setattr(track, name, self.Vector2.from_xy(*point))
            changed.append(track)
        self._commit(lambda: self.board.update_items(changed), message)

    def move_text(self, text: Text, point: tuple, message: str) -> None:
        item = self.raw[text.id]
        if text.kind == "reference":
            item.reference_field.text.position = self.Vector2.from_xy(*point)
        else:
            item.position = self.Vector2.from_xy(*point)
        self._commit(lambda: self.board.update_items(item), message)


def connect(pid: str | None):
    """The board of exactly one PCB Editor, refusing when which one is unclear."""
    import kicad_route as kr
    try:
        window = kr.pick_editor(pid)
    except kr.RouteFailure as failure:
        raise EditFailure("which_editor", failure.reason) from failure
    own = pathlib.Path(f"/tmp/kicad/api-{window['pid']}.sock")
    editors = [w for w in mac.windows(every=True) if w["app"] == "PCB Editor"]
    if not own.exists() and len({w["pid"] for w in editors}) > 1:
        raise EditFailure("which_editor", f"pid {window['pid']} has no socket of its own and several PCB Editors are running: "
                          "the shared socket could be another's")
    try:
        board = KipyEditBoard(str(window["pid"]))
    except Exception as error:
        raise EditFailure("ipc_failed", f"{type(error).__name__}: {error}") from error
    board.wait_ready()
    name = pathlib.PurePosixPath(board.board.document.board_filename).stem
    if not window["title"].lstrip("*").startswith(f"{name} "):
        raise EditFailure("which_editor", f'IPC answers for {name}, but the window says "{window["title"]}"')
    return board


def parse_args(argv: list) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Exact edits in the open PCB Editor on the Mac, over IPC.")
    parser.add_argument("--pid", help="the PCB Editor to use, when several are open")
    verbs = parser.add_subparsers(dest="verb", required=True)
    p = verbs.add_parser("list", help="a net's tracks, arcs and vias, or the board's texts and reference fields")
    what = p.add_mutually_exclusive_group(required=True)
    what.add_argument("--net")
    what.add_argument("--texts", action="store_true")
    p.add_argument("--box", nargs=4, type=float, metavar=("X0", "Y0", "X1", "Y1"))
    p = verbs.add_parser("delete", help="delete tracks, arcs and vias by id, all or none")
    p.add_argument("ids", nargs="+")
    p = verbs.add_parser("move-via", help="move a via and the track ends on it")
    p.add_argument("id")
    p.add_argument("x", type=float)
    p.add_argument("y", type=float)
    p = verbs.add_parser("move-text", help="move a board text, or with --ref a reference field")
    p.add_argument("numbers", nargs="+", metavar="ID X Y")
    p.add_argument("--ref")
    args = parser.parse_args(argv)
    if args.verb == "move-text":
        try:
            *key, x, y = args.numbers
            args.x, args.y = float(x), float(y)
        except ValueError:
            parser.error("move-text takes ID X Y, or --ref REF X Y, with X and Y in mm")
        if len(key) != (0 if args.ref else 1):
            parser.error("move-text takes ID X Y, or --ref REF X Y")
        args.id = key[0] if key else None
    return args


def run(args: argparse.Namespace, board) -> dict:
    edit = Edit(board)
    if args.verb == "list":
        return edit.list_texts(args.box) if args.texts else edit.list_net(args.net, args.box)
    if args.verb == "delete":
        return edit.delete(args.ids)
    if args.verb == "move-via":
        return edit.move_via(args.id, args.x, args.y)
    return edit.move_text(args.id, args.ref, args.x, args.y)


def main(argv: list | None = None, board=None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        if board is None and not mac.LOCAL:
            raise EditFailure("bad_request", "run this on the Mac, with .venv-kicad-ipc/bin/python, where the editor is")
        board = board or connect(args.pid)
        result = run(args, board)
    except EditFailure as failure:
        result = {"ok": False, "verb": args.verb, "status": failure.status, "reason": failure.reason}
    except mac.MacError as error:
        result = {"ok": False, "verb": args.verb, "status": "mac_failed", "reason": str(error)}
    except Exception as error:  # kipy's ApiError and the like: an edit that did not go through as asked
        result = {"ok": False, "verb": args.verb, "status": "operation_failed", "reason": f"{type(error).__name__}: {error}"}
    if board is not None and "board" not in result:
        try:
            result["board"] = board.file()
        except Exception:
            pass
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        mac.report_routing_handoff()
