#!/usr/bin/env python3
"""Offline unit tests for deterministic helper tools: part-fetch, placement-snapshot, quote-package."""
from __future__ import annotations

import datetime
import io
import json
import pathlib
import sys
import tempfile
import types
import unittest
from unittest import mock

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import board_graph_actions  # noqa: E402
import board_graph_cli  # noqa: E402
import board_part_fetch  # noqa: E402
import board_part_search  # noqa: E402
import board_placement_snapshot  # noqa: E402
import board_pre_schematic  # noqa: E402
import board_quote_package  # noqa: E402
import board_sync_models  # noqa: E402


class DeterministicToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._td.name)

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_cli_registers_three_deterministic_commands(self) -> None:
        for cmd, script in (
            ("part-search", "scripts/board_part_search.py"),
            ("part-fetch", "scripts/board_part_fetch.py"),
            ("sync-models", "scripts/board_sync_models.py"),
            ("placement-snapshot", "scripts/board_placement_snapshot.py"),
            ("quote-package", "scripts/board_quote_package.py"),
        ):
            self.assertEqual(board_graph_cli.SUBCOMMANDS[cmd], script)

    def test_part_fetch_writes_valid_pre_schematic_listings_and_parts_json(self) -> None:
        sample = {
            "data": {
                "componentBrandEn": "Heling",
                "componentModelEn": "MJ56881-B011-HLRN1-C",
                "componentLibraryType": "expand",
                "stockCount": 3246,
                "jlcPrices": [{"productPrice": 0.5753}],
                "componentSpecificationEn": "SMD",
                "describe": "RJ45 Jack",
                "dataManualUrl": "https://example.invalid/rj45.pdf",
            }
        }

        class FakeResp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        rec = board_part_fetch.fetch_lcsc_detail(
            "c501473", urlopen=lambda req, timeout=20: FakeResp(json.dumps(sample).encode("utf-8"))
        )
        self.assertEqual(rec["part"], "C501473")
        self.assertEqual(rec["class"], "Extended")
        now = datetime.datetime(2026, 11, 27, 20, 0, tzinfo=datetime.timezone.utc)
        listings = self.root / "board" / "reports" / "stock" / "listings.json"
        board_part_fetch.update_listings(listings, 5, [rec], now.isoformat())
        findings, _, detail = board_pre_schematic.pre_schematic_supply(
            listings, now, board_graph_actions.listing_time, board_graph_actions.LISTING_AGE_DAYS
        )
        self.assertEqual(findings, [], detail)

        sheets_dir = self.root / "hardware" / "reference"
        pdf_name = board_part_fetch.download_datasheet(
            rec["datasheet_url"], sheets_dir, rec["listed_mpn"], urlopen=lambda req, timeout=30: FakeResp(b"%PDF-1.4\n")
        )
        parts_file = sheets_dir / "parts.json"
        parts_data = board_part_fetch.update_parts_json(parts_file, [rec], {"C501473": pdf_name})
        self.assertEqual(parts_data["parts"]["MJ56881-B011-HLRN1-C"]["datasheet"], "MJ56881-B011-HLRN1-C.pdf")

    def test_part_fetch_prices_js_redirect_and_lcsc_fallback(self) -> None:
        self.assertEqual(board_part_fetch._first_price({"prices": [{"productPrice": 0.123}]}), 0.123)
        self.assertEqual(board_part_fetch._first_price({"initialPrice": 0.456}), 0.456)
        sheets_dir = self.root / "hardware" / "reference"
        sheets_dir.mkdir(parents=True)
        bad_pdf = sheets_dir / "Test_MPN.pdf"
        bad_pdf.write_bytes(b"<html>stale error</html>")

        def fake_urlopen(req, timeout=30):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if url == "https://bad.invalid/stub":
                return io.BytesIO(b"<script>location.href='https://good.invalid/real.pdf'</script>")
            if url == "https://good.invalid/real.pdf":
                return io.BytesIO(b"%PDF-1.5\nreal")
            if url == "https://html-only.invalid/err":
                return io.BytesIO(b"<html>404</html>")
            if "wmsc.lcsc.com" in url:
                return io.BytesIO(json.dumps({"result": {"pdfUrl": "https://good.invalid/real.pdf"}}).encode("utf-8"))
            raise AssertionError(f"unexpected URL {url}")

        name = board_part_fetch.download_datasheet("https://bad.invalid/stub", sheets_dir, "Test-MPN", urlopen=fake_urlopen)
        self.assertEqual(name, "Test-MPN.pdf")
        self.assertTrue((sheets_dir / "Test-MPN.pdf").read_bytes().startswith(b"%PDF-"))
        with self.assertRaises(ValueError):
            board_part_fetch.download_datasheet("https://html-only.invalid/err", sheets_dir, "Bad-MPN", urlopen=fake_urlopen)

        rec = {
            "part": "C2883805",
            "listed_mpn": "Hdr_MPN",
            "datasheet_url": "https://html-only.invalid/err",
            "datasheet_access_url": "",
        }
        fb_name = board_part_fetch.fetch_datasheet(rec, sheets_dir, urlopen=fake_urlopen)
        self.assertEqual(fb_name, "Hdr_MPN.pdf")
        self.assertEqual(rec["datasheet_url"], "https://good.invalid/real.pdf")

    def test_bom_accepts_mpn_property_fallback(self) -> None:
        sch = """(kicad_sch (version 20250114)
  (symbol (lib_id "Conn:J") (at 100 100 0)
    (property "Reference" "J1") (property "Value" "MJ56881")
    (property "Manufacturer" "Heling") (property "MPN" "MJ56881-B011")
    (property "Supplier" "LCSC") (property "Supplier Part" "C501473")))"""
        pcb = """(kicad_pcb (version 20250114)
  (footprint "lib:J" (layer "F.Cu") (attr smd)
    (property "Reference" "J1") (property "Value" "MJ56881")
    (property "Manufacturer" "Heling") (property "MPN" "MJ56881-B011")
    (property "Supplier" "LCSC") (property "Supplier Part" "C501473")
    (property "LCSC Part #" "C501473")))"""
        self.write_text = lambda rel, txt: (
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True),
            (self.root / rel).write_text(txt, encoding="utf-8"),
        )
        self.write_text(board_graph_actions.GRAPH, '{"nodes": []}')
        self.write_text(board_graph_actions.SCHEMATIC, sch)
        self.write_text(board_graph_actions.BOARD, pcb)
        self.write_text(
            board_graph_actions.BOM,
            '"Refs","Qty","Value","Manufacturer","MPN","Supplier","Supplier Part"\n'
            '"J1","1","MJ56881","Heling","MJ56881-B011","LCSC","C501473"\n',
        )
        proj = board_graph_actions.Project(self.root)
        report = board_graph_actions.bom_report(proj)
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["rows"][0]["mpn"], "MJ56881-B011")

    def test_part_search_sync_models_and_silkscreen_minimum_checks(self) -> None:
        sample = {
            "data": {
                "componentPageInfo": {
                    "list": [
                        {
                            "componentCode": "C501473",
                            "componentBrandEn": "Heling",
                            "componentModelEn": "MJ56881-B011-HLRN1-C",
                            "componentLibraryType": "expand",
                            "stockCount": 3246,
                            "initialPrice": 0.5753,
                            "componentSpecificationEn": "SMD",
                            "describe": "RJ45 8P8C",
                        },
                        {
                            "componentCode": "C999999",
                            "componentBrandEn": "LowStock",
                            "componentModelEn": "LOW-STOCK",
                            "componentLibraryType": "base",
                            "stockCount": 10,
                            "initialPrice": 0.10,
                        },
                    ]
                }
            }
        }
        rows = board_part_search.parse_search_results(sample, min_stock=50)
        self.assertEqual([r["part"] for r in rows], ["C501473"])
        self.assertEqual(rows[0]["unit_price_usd"], 0.5753)

        models_dir = self.root / "board" / "models"
        models_dir.mkdir(parents=True)
        step = models_dir / "MJ56881-B011-HLRN1-C.step"
        step.write_text("ISO-10303-21;\n", encoding="utf-8")
        self.assertEqual(board_sync_models.match_model_file(models_dir, mpn="MJ56881-B011-HLRN1-C"), step)
        pcb = """(kicad_pcb
  (footprint "lib:MJ56881" (layer "F.Cu")
    (property "Reference" "J1" (at 0 0) (layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15))))
    (fp_text user "tiny" (at 0 0) (layer "F.SilkS") (effects (font (size 0.8 0.8) (thickness 0.12)))))
  (gr_text "big" (at 0 0) (layer "B.SilkS") (effects (font (size 1.2 1.2) (thickness 0.18))))
  (gr_text "hidden" (at 0 0) (layer "F.SilkS") (effects (font (size 0.5 0.5) (thickness 0.08)) (hide yes))))"""
        warnings = board_placement_snapshot.silkscreen_minimum_findings(pcb)
        self.assertEqual(len(warnings), 1)
        self.assertIn("tiny", warnings[0])

    def test_placement_snapshot_matches_placement_findings(self) -> None:
        pcb = """(kicad_pcb
  (footprint "lib:RJ45" (layer "F.Cu") (at 116 110.45 180)
    (property "Reference" "J1" (at 0 0) (layer "F.SilkS")))
  (footprint "lib:HDR" (layer "B.Cu") (at 116 124.5)
    (property "Reference" "J2" (at 0 0) (layer "B.SilkS")))
)"""
        placement = board_placement_snapshot.extract_placement(pcb)
        self.assertEqual(list(placement["parts"]), ["J1", "J2"])
        placement_file = self.root / "board" / "owner_placement.json"
        placement_file.parent.mkdir(parents=True)
        placement_file.write_text(json.dumps(placement), encoding="utf-8")

        fps = {}
        for fp in board_graph_actions.children(board_graph_actions.parse(pcb), "footprint"):
            fps[str(board_graph_actions.prop(fp, "Reference"))] = fp
        fake_proj = types.SimpleNamespace(root=self.root, fps=fps)
        with mock.patch.object(board_graph_actions, "PLACEMENT", "board/owner_placement.json"):
            self.assertEqual(board_graph_actions.placement_findings(fake_proj), [])

    def test_quote_package_checksums_and_gitattributes(self) -> None:
        out = self.root / "provisional"
        out.mkdir()
        (out / "RJ45_R1.zip").write_bytes(b"PK\x03\x04zipdata")
        (out / "RJ45_R1_SMT_bom.csv").write_bytes(b"Designator\r\nJ1\r\n")
        attrs, sums = board_quote_package.write_checksums(out)
        self.assertIn("*.csv -text !eol", attrs.read_text(encoding="utf-8"))
        text = sums.read_text(encoding="utf-8")
        self.assertIn("RJ45_R1.zip", text)
        self.assertIn("RJ45_R1_SMT_bom.csv", text)


if __name__ == "__main__":
    unittest.main()
