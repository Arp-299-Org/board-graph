#!/usr/bin/env python3
"""Behavior checks for a self-contained disposable KiCad project copy."""

from __future__ import annotations

import json
import pathlib
import shutil
import tempfile
import unittest

from create_disposable_project_copy import copy_project


ROOT = pathlib.Path(__file__).resolve().parents[2]
TEST_ROOT = ROOT / "tmp" / "disposable-copy-tests"


class DisposableCopyTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_ROOT.mkdir(parents=True, exist_ok=True)
        self.work = pathlib.Path(tempfile.mkdtemp(prefix="case-", dir=TEST_ROOT)).resolve()
        self.source = self.work / "source"
        self.source.mkdir()
        self.destination = self.work / "copy"
        self.project = {
            "meta": {"filename": "original.kicad_pro"},
            "schematic": {
                "top_level_sheets": [{"filename": "original.kicad_sch", "name": "original"}]
            },
            "board": {
                "design_settings": {
                    "drc_exclusions": [["lib_footprint_mismatch|example", "Keep this comment"]]
                }
            },
        }
        self.write("original.kicad_pro", json.dumps(self.project).encode())
        self.write("original.kicad_sch", b"schematic")
        self.write("original.kicad_pcb", b"board")
        self.write("original.kicad_dru", b'(version 1)\n(rule "custom")\n')
        self.write("fp-lib-table", b'${KIPRJMOD}/local.pretty')
        self.write("local.pretty/part.kicad_mod", b"footprint")
        self.write("sym-lib-table", b'${KIPRJMOD}/local.kicad_sym')
        self.write("local.kicad_sym", b"symbol")

    def tearDown(self) -> None:
        # Only remove the unique directory created by this test, never its parent.
        self.assertEqual(self.work.parent, TEST_ROOT.resolve())
        self.assertTrue(self.work.name.startswith("case-"))
        self.assertFalse(self.work.is_symlink())
        shutil.rmtree(self.work)

    def write(self, name: str, data: bytes) -> None:
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def source_files(self) -> dict[str, bytes]:
        return {
            str(path.relative_to(self.source)): path.read_bytes()
            for path in self.source.rglob("*")
            if path.is_file()
        }

    def test_renamed_copy_keeps_rules_libraries_and_exclusions(self) -> None:
        before = self.source_files()
        manifest = copy_project(self.source, "original", self.destination, "practice")
        copied_project = json.loads((self.destination / "practice.kicad_pro").read_text())

        self.assertEqual(copied_project["meta"]["filename"], "practice.kicad_pro")
        self.assertEqual(
            copied_project["schematic"]["top_level_sheets"],
            [{"filename": "practice.kicad_sch", "name": "practice"}],
        )
        self.assertEqual(copied_project["board"], self.project["board"])
        self.assertEqual((self.destination / "practice.kicad_sch").read_bytes(), b"schematic")
        self.assertEqual((self.destination / "practice.kicad_pcb").read_bytes(), b"board")
        self.assertEqual((self.destination / "practice.kicad_dru").read_bytes(), before["original.kicad_dru"])
        self.assertFalse((self.destination / "original.kicad_dru").exists())
        for name in ("fp-lib-table", "local.pretty/part.kicad_mod", "sym-lib-table", "local.kicad_sym"):
            self.assertEqual((self.destination / name).read_bytes(), before[str(pathlib.Path(name))])
        self.assertIn("practice.kicad_dru", [entry["path"] for entry in manifest["files"]])
        self.assertEqual(self.source_files(), before)

    def test_rules_file_is_optional(self) -> None:
        (self.source / "original.kicad_dru").unlink()
        copy_project(self.source, "original", self.destination, "practice")
        self.assertTrue((self.destination / "practice.kicad_pcb").is_file())
        self.assertFalse((self.destination / "practice.kicad_dru").exists())

    def test_collision_is_rejected_before_copying(self) -> None:
        self.write("practice.kicad_dru", b"another file")
        before = self.source_files()
        with self.assertRaises(FileExistsError):
            copy_project(self.source, "original", self.destination, "practice")
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.source_files(), before)

    def test_destination_inside_source_is_rejected_before_creation(self) -> None:
        nested = self.source / "new" / "copy"
        before = self.source_files()
        with self.assertRaises(ValueError):
            copy_project(self.source, "original", nested, "practice")
        self.assertFalse(nested.parent.exists())
        self.assertEqual(self.source_files(), before)

    def test_project_base_cannot_escape_destination(self) -> None:
        for base in ("../outside", "D:outside"):
            with self.subTest(base=base):
                with self.assertRaises(ValueError):
                    copy_project(self.source, "original", self.destination, base)
                self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()
