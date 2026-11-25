"""A batch answers for every copper and position change: its own nets, declared fixes, or a failure."""
import unittest

from board_copper_diff import compare

BASE = """(kicad_pcb
	(footprint "lib:R" (layer "F.Cu") (at 10 10) (property "Reference" "R1"))
	(segment (start 0 0) (end 1 0) (width 0.25) (layer "F.Cu") (net "/OLD") (uuid "a"))
	(zone (net "GND") (layers "In1.Cu") (uuid "z") (polygon (pts (xy 0 0) (xy 5 0) (xy 5 5)))
		(filled_polygon (layer "In1.Cu") (pts (xy 0 0) (xy 5 0) (xy 5 5))))
)"""


def board(*extra, at="10 10", zone_fill="(xy 0 0) (xy 5 0) (xy 5 5)"):
    text = BASE.replace("(at 10 10)", f"(at {at})").replace(
        "(filled_polygon (layer \"In1.Cu\") (pts (xy 0 0) (xy 5 0) (xy 5 5)))",
        f"(filled_polygon (layer \"In1.Cu\") (pts {zone_fill}))")
    return text[:-1] + "".join(extra) + ")"


NEW = '(segment (start 1 0) (end 2 0) (width 0.25) (layer "F.Cu") (net "/NEW") (uuid "b"))'
VIA = '(via (at 2 0) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "/NEW") (uuid "c"))'


class CopperDiffTests(unittest.TestCase):
    def test_batch_nets_and_refills_pass(self):
        report = compare(BASE, board(NEW, VIA, zone_fill="(xy 0 0) (xy 4 0) (xy 4 4)"), {"NEW"}, {})
        self.assertEqual([], report["undeclared"])
        self.assertEqual({"segment": 1, "via": 1}, report["batch"][0]["added"])

    def test_change_outside_the_batch_fails_until_declared(self):
        moved = board().replace("(end 1 0)", "(end 1 1)")
        report = compare(BASE, moved, {"NEW"}, {})
        self.assertEqual(["OLD"], [r["name"] for r in report["undeclared"]])
        report = compare(BASE, moved, {"NEW"}, {"OLD": "rerouted around the new via"})
        self.assertEqual([], report["undeclared"])
        self.assertEqual("rerouted around the new via", report["fixes"][0]["reason"])

    def test_earlier_copper_moved_on_a_batch_net_fails_until_declared(self):
        moved = board('(segment (start 1 1) (end 3 1) (width 0.25) (layer "F.Cu") (net "/OLD") (uuid "d"))').replace(
            "(end 1 0)", "(end 1 1)")
        report = compare(BASE, moved, {"OLD"}, {})
        self.assertEqual([("OLD", {"segment": 1})], [(r["name"], r["removed"]) for r in report["undeclared"]])
        self.assertEqual(["segment F.Cu (0.0, 0.0) to (1.0, 0.0)"], report["undeclared"][0]["earlier"])
        report = compare(BASE, moved, {"OLD"}, {"OLD": "moved off the new via"})
        self.assertEqual([], report["undeclared"])
        self.assertEqual({"segment": 2}, report["batch"][0]["added"])
        self.assertEqual("moved off the new via", report["fixes"][0]["reason"])

    def test_track_split_for_a_branch_is_not_a_change_but_a_shortened_one_is(self):
        piece = '(segment (start {}) (end {}) (width 0.25) (layer "F.Cu") (net "/OLD") (uuid "{}"))'
        short = board().replace("(end 1 0)", "(end 0.4 0)")[:-1]
        split = short + piece.format("0.4 0", "1 0", "e") + piece.format("0.4 0", "0.4 1", "f") + ")"
        report = compare(BASE, split, {"OLD"}, {})
        self.assertEqual([], report["undeclared"])
        self.assertEqual({"segment": 1}, report["batch"][0]["split"])
        shortened = short + piece.format("0.4 0", "0.4 1", "f") + ")"
        self.assertEqual(["OLD"], [r["name"] for r in compare(BASE, shortened, {"OLD"}, {})["undeclared"]])

    def test_reshaped_rule_area_answers_by_its_name(self):
        area = '(zone (net "") (name "{}") (layers "F.Cu" "B.Cu") (uuid "k") (polygon (pts (xy 0 0) (xy {} 0) (xy 1 1))))'
        before, after = board(area.format("U9 isolation", 1)), board(area.format("U9 isolation", 2))
        self.assertEqual([("area", "U9 isolation")], [(r["kind"], r["name"]) for r in compare(before, after, set(), {})["undeclared"]])
        self.assertEqual([], compare(before, after, set(), {"U9 isolation": "widened to the pad rows"})["undeclared"])

    def test_moved_part_and_unused_fix_are_undeclared(self):
        report = compare(BASE, board(at="11 10"), set(), {"OLD": "nothing done"})
        self.assertEqual({("part", "R1"), ("fix", "OLD")}, {(r["kind"], r["name"]) for r in report["undeclared"]})

    def test_track_direction_does_not_count_as_a_change(self):
        reversed_track = board().replace("(start 0 0) (end 1 0)", "(start 1 0) (end 0 0)")
        self.assertEqual({"batch": [], "fixes": [], "undeclared": []}, compare(BASE, reversed_track, set(), {}))


if __name__ == "__main__":
    unittest.main()
