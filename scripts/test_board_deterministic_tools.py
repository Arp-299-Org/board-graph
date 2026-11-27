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
import board_placement_snapshot  # noqa: E402
import board_pre_schematic  # noqa: E402
import board_quote_package  # noqa: E402


class DeterministicToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._td.name)

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_cli_registers_three_deterministic_commands(self) -> None:
        for cmd, script in (
            ("part-fetch", "scripts/board_part_fetch.py"),
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
