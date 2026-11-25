import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import board_context  # noqa: E402


KICAD10_SAMPLE = """(kicad_pcb
  (version 20260206)
  (generator "pcbnew")
  (layers
    (0 "F.Cu" signal)
    (2 "B.Cu" signal)
  )
  (footprint "j:J1"
    (layer "F.Cu")
    (at 110.5 103.815 180)
    (property "Reference" "J1" (at 0 0 0) (layer "F.SilkS"))
    (fp_line (start 10.48 3.82) (end -10.48 3.82) (layer "F.CrtYd"))
    (fp_line (start -10.48 -3.8) (end 10.48 -3.8) (layer "F.CrtYd"))
    (pad "" smd rect (at -8.65 0.295 180) (size 1.25 1.8) (layers "F.Cu" "F.Mask"))
    (pad "1" smd roundrect (at -6.75 -2.795 180) (size 0.6 1) (layers "F.Cu" "F.Mask") (roundrect_rratio 0.25) (net "/M1_A"))
    (pad "5" smd roundrect (at -0.75 -2.795 180) (size 0.6 1) (layers "F.Cu" "F.Mask") (roundrect_rratio 0.25) (net "/M3_A"))
  )
  (footprint "j:J2"
    (layer "F.Cu")
    (at 110.5 117.95)
    (property "Reference" "J2" (at 0 0 0) (layer "F.SilkS"))
    (fp_line (start -8.3 -2.1) (end 8.3 -2.1) (layer "F.CrtYd"))
    (fp_line (start 8.3 4) (end -8.3 4) (layer "F.CrtYd"))
    (pad "1" smd rect (at -4.75 -1.35) (size 0.3 1) (layers "F.Cu" "F.Mask") (net "/M1_A"))
    (pad "9" smd rect (at -0.75 -1.35) (size 0.3 1) (layers "F.Cu" "F.Mask") (net "/M3_A"))
  )
  (footprint "t:F90"
    (layer "F.Cu")
    (at 10 20 90)
    (property "Reference" "U9" (at 0 0 0) (layer "F.SilkS"))
    (fp_rect (start -2 -1) (end 4 2) (layer "F.CrtYd"))
    (pad "1" smd rect (at 3 4 90) (size 0.6 1.0) (layers "F.Cu") (net "/M9"))
  )
  (footprint "t:B90"
    (layer "B.Cu")
    (at 10 20 90)
    (property "Reference" "U8" (at 0 0 0) (layer "B.SilkS"))
    (pad "1" smd rect (at 3 4 90) (size 0.6 1.0) (layers "B.Cu") (net "/M8"))
  )
  (gr_line (start 100 100) (end 121 100) (layer "Edge.Cuts"))
  (gr_line (start 121 100) (end 121 121.7) (layer "Edge.Cuts"))
  (gr_line (start 121 121.7) (end 100 121.7) (layer "Edge.Cuts"))
  (gr_line (start 100 121.7) (end 100 100) (layer "Edge.Cuts"))
  (segment (start 117.25 106.61) (end 117.25 108.55) (width 0.5) (layer "F.Cu") (net "/M1_A"))
  (via (at 117.25 108.55) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "/M1_A"))
  (segment (start 111.25 106.61) (end 111.25 107.11) (width 0.5) (layer "F.Cu") (net "/M3_A"))
)
"""


KICAD8_SAMPLE = """(kicad_pcb
  (version 20240108)
  (generator "pcbnew")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
  (net 1 "/SIG1")
  (footprint "t:Zero"
    (layer "F.Cu")
    (at 25 30 0)
    (fp_text reference "R1" (at 0 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at -1 0 0) (size 0.8 1.2) (layers "F.Cu") (net 1 "/SIG1"))
  )
  (footprint "t:Zero"
    (layer "F.Cu")
    (at 35 30 0)
    (fp_text reference "R2" (at 0 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 1 0 0) (size 0.8 1.2) (layers "F.Cu") (net 1))
  )
  (segment (start 24 30) (end 26 30) (width 0.25) (layer "F.Cu") (net 1))
  (via (at 26 30) (size 0.5) (drill 0.25) (layers "F.Cu" "B.Cu") (net 1))
)
"""


class BoardContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)

    def test_empty_or_minimal_board_returns_empty_strings(self):
        p = self.root / "empty.kicad_pcb"
        p.write_text("(kicad_pcb)", encoding="utf-8")
        self.assertEqual(board_context.render_board_context(p), ("", ""))
        p.write_text("(kicad_pcb (layers (0 \"F.Cu\" signal) (2 \"B.Cu\" signal)))", encoding="utf-8")
        self.assertEqual(board_context.render_board_context(p), ("", ""))
        self.assertEqual(board_context.render_board_context(self.root / "missing.kicad_pcb"), ("", ""))

    def test_kicad10_transforms_courtyards_rules_escapes_and_copper(self):
        pcb = self.root / "joiner.kicad_pcb"
        pro = self.root / "joiner.kicad_pro"
        dru = self.root / "joiner.kicad_dru"
        pcb.write_text(KICAD10_SAMPLE, encoding="utf-8")
        pro.write_text(json.dumps({
            "net_settings": {
                "classes": [{
                    "name": "Default",
                    "track_width": 0.5,
                    "via_diameter": 0.6,
                    "via_drill": 0.3,
                    "clearance": 0.15,
                }],
            },
            "board": {"design_settings": {"rules": {"min_track_width": 0.2, "min_clearance": 0.15}}},
        }), encoding="utf-8")
        dru.write_text('(version 1)\n(rule "R1 neck at the tab socket"\n  (constraint track_width (min 0.3mm)))\n',
                       encoding="utf-8")

        ctx = board_context.extract_board_context(pcb, [["J1.5", "J2.9", "/M3_A"]], centered=True)
        self.assertEqual(ctx["copper_layers"], ["F.Cu", "B.Cu"])
        self.assertEqual(ctx["outline"], [100.0, 100.0, 121.0, 121.7])

        # Verified KiCad 10 world pad coordinates under 0/90/180 and F.Cu/B.Cu transforms.
        self.assertEqual(ctx["pads"]["J1.1"]["center"], (117.25, 106.61))
        self.assertEqual(ctx["pads"]["J1.5"]["center"], (111.25, 106.61))
        self.assertEqual(ctx["pads"]["J2.9"]["center"], (109.75, 116.6))
        self.assertEqual(ctx["pads"]["U9.1"]["center"], (14.0, 17.0))
        self.assertEqual(ctx["pads"]["U8.1"]["center"], (14.0, 17.0))
        self.assertEqual(len(ctx["mount_pads"]), 1)

        fps = {fp["ref"]: fp for fp in ctx["footprints"]}
        self.assertEqual(fps["J2"]["crtyd_bbox"], [102.2, 115.85, 118.8, 121.95])
        self.assertEqual(fps["U9"]["crtyd_bbox"], [9.0, 16.0, 12.0, 22.0])

        t = ctx["targets"][0]
        self.assertEqual(t["escape_a"]["edge"], (111.25, 107.11))
        self.assertEqual(t["escape_b"]["edge"], (109.75, 116.1))

        inline, full = board_context.render_board_context(pcb, [["J1.5", "J2.9", "/M3_A"]], centered=True)
        self.assertEqual(inline, full)
        self.assertIn("Edge.Cuts: x=[100..121], y=[100..121.7]", inline)
        self.assertIn("y=[115.85..121.95]", inline)
        self.assertIn('R1 neck at the tab socket', inline)
        self.assertIn("track=0.5 mm, via=0.6/0.3 mm, clearance=0.15 mm", inline)
        self.assertIn("J1.5 (111.25,106.61)", inline)
        self.assertIn("short_edge=(111.25,107.11)", inline)
        self.assertIn("J1.mount (<unconnected>, smd)", inline)
        # Active copper groups Vias before F.Cu and sorts items by x inside each group.
        self.assertIn("- Vias (1): /M1_A (117.25,108.55) 0.6/0.3", inline)
        self.assertIn("- F.Cu (2): /M3_A (111.25,106.61)->(111.25,107.11) w=0.5; /M1_A (117.25,106.61)->(117.25,108.55) w=0.5", inline)
        self.assertLess(inline.index("- Vias (1):"), inline.index("- F.Cu (2):"))
        self.assertLess(inline.index("/M3_A (111.25,106.61)"), inline.index("/M1_A (117.25,106.61)"))

    def test_kicad8_net_map_and_large_board_truncation(self):
        pcb = self.root / "k8.kicad_pcb"
        pcb.write_text(KICAD8_SAMPLE, encoding="utf-8")
        ctx = board_context.extract_board_context(pcb, [["R1.1", "R2.1", "/SIG1"]])
        self.assertEqual(ctx["pads"]["R1.1"]["center"], (24.0, 30.0))
        self.assertEqual(ctx["pads"]["R2.1"]["center"], (36.0, 30.0))
        self.assertEqual(ctx["pads"]["R2.1"]["net"], "/SIG1")
        self.assertEqual({item["net"] for item in ctx["copper"]}, {"/SIG1"})

        full_ctx = self.root / "board-context.txt"
        inline, full = board_context.render_board_context(
            pcb, [["R1.1", "R2.1", "/SIG1"]], max_chars=260, full_path=full_ctx,
        )
        self.assertGreater(len(full), len(inline))
        self.assertIn(f"read full context at {full_ctx}", inline)

        inline2, _ = board_context.render_board_context(
            pcb, [["R1.1", "R2.1", "/SIG1"]], max_copper_items=1,
        )
        self.assertIn("(+1 more)", inline2)

    def test_extract_and_render_zones_and_non_rect_edge_cuts(self):
        board_text = """(kicad_pcb
  (version 20260206)
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (2 "B.Cu" signal)
  )
  (net 0 "")
  (net 1 "GND")
  (gr_line (start 0 0) (end 30 0) (layer "Edge.Cuts"))
  (gr_line (start 30 0) (end 30 10) (layer "Edge.Cuts"))
  (gr_line (start 30 10) (end 15 10) (layer "Edge.Cuts"))
  (gr_line (start 15 10) (end 15 20) (layer "Edge.Cuts"))
  (gr_line (start 15 20) (end 0 20) (layer "Edge.Cuts"))
  (gr_line (start 0 20) (end 0 0) (layer "Edge.Cuts"))
  (zone (net 1) (net_name "GND") (layer "In1.Cu") (name "gnd_plane")
    (priority 1)
    (connect_pads (clearance 0.25))
    (min_thickness 0.2)
    (polygon (pts (xy 0.5 0.5) (xy 29.5 0.5) (xy 29.5 9.5) (xy 0.5 9.5)))
    (filled_polygon (layer "In1.Cu") (pts (xy 0.5 0.5) (xy 29.5 0.5) (xy 29.5 9.5)))
  )
  (zone (net 0) (layers "F.Cu" "B.Cu") (name "keep_clear")
    (keepout (tracks not_allowed) (vias not_allowed) (pads allowed) (copperpour not_allowed))
    (polygon (pts (xy 5 5) (xy 10 5) (xy 10 10) (xy 5 10)))
  )
)
"""
        with tempfile.TemporaryDirectory() as td:
            pcb = pathlib.Path(td) / "board.kicad_pcb"
            pcb.write_text(board_text, encoding="utf-8")
            ctx = board_context.extract_board_context(pcb, [])
            self.assertEqual(len(ctx["outline_polygon"]), 6)
            self.assertEqual(len(ctx["zones"]), 2)
            pour_z = next(z for z in ctx["zones"] if z["kind"] == "pour")
            rule_z = next(z for z in ctx["zones"] if z["kind"] == "rule_area")
            self.assertEqual((pour_z["net"], pour_z["layers"], pour_z["filled"], pour_z["filled_layers"]),
                             ("GND", ["In1.Cu"], True, ["In1.Cu"]))
            self.assertEqual((rule_z["name"], rule_z["layers"]), ("keep_clear", ["F.Cu", "B.Cu"]))
            inline, _ = board_context.render_board_context(pcb, [])
            self.assertIn("Edge.Cuts polygon", inline)
            self.assertIn("### Existing zones (2 total: pours & rule areas)", inline)
            self.assertIn("pour GND (In1.Cu) name='gnd_plane': prio=1, connect=thermal, filled", inline)
            self.assertIn("rule_area (F.Cu,B.Cu) name='keep_clear'", inline)

    def test_dense_copper_spatial_box_puts_vias_first_and_never_hides_active_nets(self):
        distant_gnd = "\n".join(
            f'  (segment (start {200 + i * 0.5} 10) (end {200 + i * 0.5} 12) (width 0.25) (layer "F.Cu") (net "GND"))'
            for i in range(110)
        )
        board_text = f"""(kicad_pcb
  (version 20260206)
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (2 "B.Cu" signal)
  )
  (footprint "t:U6" (layer "F.Cu") (at 100 100)
    (property "Reference" "U6" (at 0 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 0.5 0.5) (layers "F.Cu") (net "/DATA_C"))
  )
  (footprint "t:U7" (layer "F.Cu") (at 110 100)
    (property "Reference" "U7" (at 0 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 0.5 0.5) (layers "F.Cu") (net "/DATA_C"))
  )
{distant_gnd}
  (segment (start 220 50) (end 225 50) (width 0.25) (layer "B.Cu") (net "+5V"))
  (via (at 225 50) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "+5V"))
  (via (at 105 102) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "GND"))
  (segment (start 108 99) (end 108 103) (width 0.25) (layer "F.Cu") (net "Net-(U6-Q6)"))
  (segment (start 102 104) (end 106 104) (width 0.25) (layer "B.Cu") (net "Net-(U7-1~{{RD}})"))
)
"""
        pcb = self.root / "dense.kicad_pcb"
        pcb.write_text(board_text, encoding="utf-8")
        inline, _full = board_context.render_board_context(pcb, [["U6.1", "U7.1", "/DATA_C"]])
        self.assertIn("### Existing copper (3 in active box x=[92..118], y=[92..108]; 115 total)", inline)
        self.assertIn("- Vias (1): GND (105,102) 0.6/0.3", inline)
        self.assertIn("- F.Cu (1): Net-(U6-Q6) (108,99)->(108,103) w=0.25", inline)
        self.assertIn("- B.Cu (1): Net-(U7-1~{RD}) (102,104)->(106,104) w=0.25", inline)
        self.assertLess(inline.index("- Vias (1):"), inline.index("- F.Cu (1):"))
        self.assertLess(inline.index("- F.Cu (1):"), inline.index("- B.Cu (1):"))
        self.assertIn("- Outside active box (summarized, 112 items): +5V (1 tracks, 1 vias); GND (110 tracks, 0 vias)", inline)
        self.assertNotIn("Other nets (summarized)", inline)


if __name__ == "__main__":
    unittest.main()
