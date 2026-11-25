#!/usr/bin/env python3
"""Tests of scripts/kicad_edit.py with no KiCad: argument parsing, refusals and read-back, against a stand-in board.

  py -3 scripts/test_kicad_edit.py
"""
from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import kicad_edit as ke  # noqa: E402
from kicad_edit import Copper, Text  # noqa: E402

THROUGH = ("F.Cu", "B.Cu")
V = (117_600_000, 105_200_000)  # the AMP_EN via


def track(key, start, end, net="AMP_EN", layer="F.Cu", locked=False):
    return Copper(key, "track", net, (layer,), start, end, 250_000, locked)


def via(key, at, net="AMP_EN", locked=False):
    return Copper(key, "via", net, THROUGH, at, at, 600_000, locked)


class FakeBoard:
    """The editor's board as IPC shows it. `misbehave` makes an edit land wrong, to prove the read-back."""

    def __init__(self, copper=(), texts=(), misbehave=None):
        self.items = {i.id: i for i in copper}
        self.text_items = {t.id: t for t in texts}
        self.misbehave = misbehave
        self.commits = []

    def file(self):
        return "/tmp/edit/board/demo.kicad_pcb"

    def nets(self):
        return {i.net for i in self.items.values()} | {"GND"}

    def copper(self):
        return list(self.items.values())

    def texts(self):
        return list(self.text_items.values())

    def delete(self, ids, message):
        self.commits.append(message)
        for key in ids[:-1] if self.misbehave == "keep_last" else ids:
            del self.items[key]

    def move_via(self, key, point, ends, message):
        self.commits.append(message)
        self.items[key] = dataclasses.replace(self.items[key], start=point, end=point)
        for track_key, names in ends[:-1] if self.misbehave == "leave_track" else ends:
            self.items[track_key] = dataclasses.replace(self.items[track_key], **{n: point for n in names})

    def move_text(self, text, point, message):
        self.commits.append(message)
        moved = dataclasses.replace(text, at=point)
        if self.misbehave == "drop_model":
            moved = dataclasses.replace(moved, footprint=text.footprint[:4] + (0,) + text.footprint[5:])
        self.text_items[text.id] = moved


def call(argv, board):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = ke.main(argv, board)
    return code, json.loads(out.getvalue())


class Parsing(unittest.TestCase):
    def test_verbs(self):
        args = ke.parse_args(["list", "--net", "/ENC_A", "--box", "1", "2", "3", "4"])
        self.assertEqual((args.verb, args.net, args.box), ("list", "/ENC_A", [1.0, 2.0, 3.0, 4.0]))
        self.assertEqual(ke.parse_args(["delete", "a", "b"]).ids, ["a", "b"])
        args = ke.parse_args(["--pid", "42", "move-via", "v1", "117.6", "105.7"])
        self.assertEqual((args.pid, args.id, args.x, args.y), ("42", "v1", 117.6, 105.7))
        args = ke.parse_args(["move-text", "t1", "10", "20"])
        self.assertEqual((args.id, args.ref, args.x, args.y), ("t1", None, 10.0, 20.0))
        args = ke.parse_args(["move-text", "--ref", "R44", "10", "20"])
        self.assertEqual((args.id, args.ref), (None, "R44"))

    def test_bad_arguments(self):
        for argv in (["list"], ["list", "--net", "A", "--texts"], ["delete"], ["move-via", "v1", "1"],
                     ["move-text", "t1", "x", "2"], ["move-text", "--ref", "R44", "t1", "1", "2"], ["move-text", "1", "2"],
                     ["create", "a"], ["move-footprint", "R44", "1", "2"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                ke.parse_args(argv)

    def test_rounds_to_integer_nanometres(self):
        self.assertEqual(ke.to_nm(134.2), 134_200_000)
        self.assertEqual(ke.to_nm(-0.0000004), 0)


class Listing(unittest.TestCase):
    def setUp(self):
        self.board = FakeBoard([via("v", V), track("in", (110_000_000, 105_200_000), V), track("far", (1, 1), (2, 2)),
                                track("other", (110_000_000, 100_000_000), (111_000_000, 100_000_000), net="BTN_1")],
                               [Text("t", "text", "DEMO", "F.Silkscreen", (5_000_000, 5_000_000))])

    def test_net_and_box(self):
        code, result = call(["list", "--net", "AMP_EN", "--box", "109", "104", "118", "106"], self.board)
        self.assertEqual(code, 0)
        self.assertEqual([i["id"] for i in result["items"]], ["v", "in"])
        self.assertEqual(result["items"][1]["start"], [110.0, 105.2])
        self.assertEqual(result["board"], "/tmp/edit/board/demo.kicad_pcb")

    def test_a_segment_crossing_the_box_counts(self):
        self.assertTrue(ke.in_box((0, 0, 1, 1), (-1_000_000, 500_000), (2_000_000, 500_000)))
        self.assertFalse(ke.in_box((0, 0, 1, 1), (-1_000_000, 2_000_000), (2_000_000, 2_000_000)))

    def test_unknown_net(self):
        code, result = call(["list", "--net", "AMP_ENN"], self.board)
        self.assertEqual((code, result["status"]), (1, "unknown_net"))

    def test_texts(self):
        code, result = call(["list", "--texts"], self.board)
        self.assertEqual((code, result["items"][0]["text"]), (0, "DEMO"))


class Delete(unittest.TestCase):
    def board(self, **kw):
        return FakeBoard([track("a", (0, 0), (1_000_000, 0)), track("b", (1_000_000, 0), (2_000_000, 0)),
                          track("lock", (0, 1), (1, 1), locked=True)], **kw)

    def test_deletes_in_one_commit(self):
        board = self.board()
        code, result = call(["delete", "a", "b"], board)
        self.assertEqual((code, result["ok"]), (0, True))
        self.assertEqual(set(board.items), {"lock"})
        self.assertEqual(len(board.commits), 1)
        self.assertIsNone(result["items"][0]["after"])

    def test_all_or_nothing(self):
        board = self.board()
        code, result = call(["delete", "a", "nope"], board)
        self.assertEqual((code, result["status"]), (1, "not_found"))
        self.assertEqual(set(board.items), {"a", "b", "lock"})
        self.assertEqual(board.commits, [])

    def test_only_copper(self):
        board = FakeBoard([track("a", (0, 0), (1, 0))], [Text("t", "text", "X", "F.Silkscreen", (0, 0))])
        code, result = call(["delete", "t"], board)
        self.assertEqual((code, result["status"], board.commits), (1, "not_found", []))

    def test_locked_refused(self):
        board = self.board()
        code, result = call(["delete", "a", "lock"], board)
        self.assertEqual((code, result["status"], len(board.items)), (1, "locked", 3))

    def test_read_back_failing(self):
        code, result = call(["delete", "a", "b"], self.board(misbehave="keep_last"))
        self.assertEqual((code, result["ok"], result["status"]), (1, False, "read_back_differs"))
        self.assertIn("track b is still on the board", result["reason"])


class MoveVia(unittest.TestCase):
    def setUp(self):
        self.copper = [
            via("v", V),
            track("top", (110_000_000, 105_200_000), V),  # ends on it: follows
            track("bottom", V, (117_600_000, 110_000_000), layer="B.Cu"),  # starts on it, other side: follows
            track("passing", (117_600_000, 100_000_000), (117_600_000, 104_000_000)),  # same net, ends elsewhere
            track("stranger", (117_000_000, 105_200_000), V, net="BTN_1"),  # other net at the same point: stays
            track("inner", (100_000_000, 100_000_000), V, layer="In1.Cu"),  # a layer the via spans? not in THROUGH
        ]

    def test_followers(self):
        board = FakeBoard(self.copper)
        code, result = call(["move-via", "v", "117.6", "105.7"], board)
        self.assertEqual((code, result["ok"]), (0, True), result)
        new = (117_600_000, 105_700_000)
        self.assertEqual(board.items["v"].start, new)
        self.assertEqual(board.items["top"].end, new)
        self.assertEqual(board.items["top"].start, (110_000_000, 105_200_000))
        self.assertEqual(board.items["bottom"].start, new)
        for unmoved in ("passing", "stranger", "inner"):
            self.assertEqual(board.items[unmoved], next(i for i in self.copper if i.id == unmoved))
        self.assertEqual(result["track_ends_moved"], 2)
        self.assertEqual(len(board.commits), 1)
        self.assertEqual({i["id"] for i in result["items"]}, {"v", "top", "bottom"})

    def test_a_point_rounds_to_the_nanometre(self):
        board = FakeBoard([via("v", V)])
        call(["move-via", "v", "134.2", "105.2"], board)
        self.assertEqual(board.items["v"].start, (134_200_000, 105_200_000))

    def test_off_centre_end_refused(self):
        board = FakeBoard(self.copper + [track("near", (100_000_000, 0), (117_650_000, 105_200_000))])
        code, result = call(["move-via", "v", "117.6", "105.7"], board)
        self.assertEqual((code, result["status"], board.commits), (1, "would_leave_copper", []))
        self.assertIn("near", result["reason"])

    def test_arc_end_refused(self):
        arc = Copper("arc", "arc", "AMP_EN", ("F.Cu",), (100_000_000, 0), V, 250_000, False, (110_000_000, 50_000_000))
        board = FakeBoard(self.copper + [arc])
        code, result = call(["move-via", "v", "117.6", "105.7"], board)
        self.assertEqual((code, result["status"], board.commits), (1, "would_leave_copper", []))

    def test_not_a_via(self):
        code, result = call(["move-via", "top", "1", "1"], FakeBoard(self.copper))
        self.assertEqual((code, result["status"]), (1, "not_found"))

    def test_locked_track_on_it_refused(self):
        board = FakeBoard([via("v", V), track("t", (0, 0), V, locked=True)])
        code, result = call(["move-via", "v", "1", "1"], board)
        self.assertEqual((code, result["status"], board.commits), (1, "locked", []))

    def test_read_back_failing(self):
        code, result = call(["move-via", "v", "117.6", "105.7"], FakeBoard(self.copper, misbehave="leave_track"))
        self.assertEqual((code, result["status"]), (1, "read_back_differs"))
        self.assertIn("bottom", result["reason"])


class MoveText(unittest.TestCase):
    def texts(self):
        return [Text("t", "text", "DEMO REV1", "F.Silkscreen", (5_000_000, 5_000_000)),
                Text("fp44", "reference", "R44", "F.Silkscreen", (10_000_000, 10_000_000), ref="R44",
                     footprint=(11_000_000, 10_000_000, 90.0, "F.Cu", 1, 7))]

    def test_board_text(self):
        board = FakeBoard([track("a", (0, 0), (1, 0))], self.texts())
        code, result = call(["move-text", "t", "6", "5.5"], board)
        self.assertEqual((code, board.text_items["t"].at), (0, (6_000_000, 5_500_000)))
        self.assertEqual(result["items"][0]["before"]["at"], [5.0, 5.0])

    def test_reference(self):
        board = FakeBoard([], self.texts())
        code, result = call(["move-text", "--ref", "R44", "10.5", "10"], board)
        self.assertEqual((code, result["ok"]), (0, True))
        self.assertEqual(board.text_items["fp44"].footprint, self.texts()[1].footprint)

    def test_footprint_id_needs_ref(self):
        code, result = call(["move-text", "fp44", "1", "1"], FakeBoard([], self.texts()))
        self.assertEqual((code, result["status"]), (1, "not_found"))
        self.assertIn("--ref", result["reason"])

    def test_no_such_reference(self):
        code, result = call(["move-text", "--ref", "R99", "1", "1"], FakeBoard([], self.texts()))
        self.assertEqual((code, result["status"]), (1, "not_found"))

    def test_footprint_changed_is_caught(self):
        code, result = call(["move-text", "--ref", "R44", "10.5", "10"], FakeBoard([], self.texts(), misbehave="drop_model"))
        self.assertEqual((code, result["status"]), (1, "read_back_differs"))


class WhichEditor(unittest.TestCase):
    def test_busy(self):
        class Route:
            def wait_ready(self):
                raise RuntimeError("KiCad stayed busy: a tool or dialog is holding it")
        board = ke.KipyEditBoard.__new__(ke.KipyEditBoard)
        board.route = Route()
        with self.assertRaises(ke.EditFailure) as caught:
            board.copper()
        self.assertEqual(caught.exception.status, "busy")

    def test_no_single_editor(self):
        import kicad_route as kr
        with patch.object(kr, "pick_editor", side_effect=kr.RouteFailure("bad_request", "2 PCB Editor windows")):
            with self.assertRaises(ke.EditFailure) as caught:
                ke.connect(None)
        self.assertEqual(caught.exception.status, "which_editor")

    def test_shared_socket_with_several_editors(self):
        import kicad_route as kr
        window = {"pid": 999999, "number": 1, "title": "demo PCB Editor", "app": "PCB Editor"}
        other = dict(window, pid=999998, number=2)
        with patch.object(kr, "pick_editor", return_value=window), patch.object(ke.mac, "windows", return_value=[window, other]):
            with self.assertRaises(ke.EditFailure) as caught:
                ke.connect("999999")
        self.assertEqual(caught.exception.status, "which_editor")
        self.assertIn("socket", caught.exception.reason)


class Elsewhere(unittest.TestCase):
    def test_refuses_off_the_mac(self):
        if ke.mac.LOCAL:
            self.skipTest("this is the Mac")
        code, result = call(["list", "--texts"], None)
        self.assertEqual((code, result["status"]), (1, "bad_request"))


if __name__ == "__main__":
    unittest.main()
