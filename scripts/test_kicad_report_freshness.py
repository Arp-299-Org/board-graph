"""Report stamping handles JSON arrays without bypassing freshness checks."""
import contextlib
import datetime
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

import kicad_report_freshness as freshness


class ReportFreshnessTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="bg-freshness-")
        self.addCleanup(temp.cleanup)
        self.reports = pathlib.Path(temp.name) / "reports"
        self.reports.mkdir()
        self.board = pathlib.Path(temp.name) / "test.kicad_pcb"
        self.schematic = self.board.with_suffix(".kicad_sch")
        self.board.write_text("board 1", encoding="utf-8")
        self.schematic.write_text("schematic 1", encoding="utf-8")
        self.board_time = 1700000100
        os.utime(self.board, (self.board_time, self.board_time))
        self.report = self.reports / "report.json"
        self.write_report([])

    def write_report(self, data, timestamp=None):
        self.report.write_text(json.dumps(data), encoding="utf-8")
        timestamp = self.board_time + 10 if timestamp is None else timestamp
        os.utime(self.report, (timestamp, timestamp))

    def run_action(self, action, *args):
        argv = ["kicad_report_freshness.py", action, "--reports", str(self.reports),
                "--board", str(self.board), *args]
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            result = freshness.main()
        return result, stdout.getvalue(), stderr.getvalue()

    def test_fresh_array_can_be_stamped_and_checked(self):
        result, _, _ = self.run_action("stamp", "--report", self.report.name)
        self.assertEqual(result, 0)
        result, stdout, _ = self.run_action("check", "--require", self.report.name)
        self.assertEqual(result, 0)
        self.assertIn("fresh    report.json", stdout)

    def test_array_older_than_board_cannot_be_stamped(self):
        self.write_report([], self.board_time - 10)
        result, _, stderr = self.run_action("stamp", "--report", self.report.name)
        self.assertEqual(result, 2)
        self.assertIn("before the board", stderr)
        self.assertFalse((self.reports / freshness.PROVENANCE_NAME).exists())

    def test_stale_declared_object_date_cannot_be_stamped_even_with_new_file_mtime(self):
        old_date = datetime.datetime.fromtimestamp(self.board_time - 60).isoformat()
        self.write_report({"date": old_date})
        result, _, stderr = self.run_action("stamp", "--report", self.report.name)
        self.assertEqual(result, 2)
        self.assertIn("says it was generated", stderr)
        self.assertFalse((self.reports / freshness.PROVENANCE_NAME).exists())

    def test_object_dates_keep_existing_parsing(self):
        date = datetime.datetime.fromtimestamp(self.board_time + 60)
        self.write_report({"date": date.isoformat()})
        self.assertEqual(freshness.self_declared_date(self.report), date)
        self.assertEqual(self.run_action("stamp", "--report", self.report.name)[0], 0)
        self.write_report({"date": "not a date"})
        self.assertIsNone(freshness.self_declared_date(self.report))

    def test_non_object_json_has_no_declared_date(self):
        for value in ([], [1], "text", 42, True, None):
            with self.subTest(value=value):
                self.write_report(value)
                self.assertIsNone(freshness.self_declared_date(self.report))

    def test_changed_board_or_schematic_hash_is_stale(self):
        self.assertEqual(self.run_action("stamp", "--report", self.report.name)[0], 0)
        for path, original in ((self.board, "board 1"), (self.schematic, "schematic 1")):
            with self.subTest(source=path.name):
                path.write_text(original + " changed", encoding="utf-8")
                result, _, stderr = self.run_action("check", "--require", self.report.name)
                self.assertEqual(result, 1)
                self.assertIn("(stale)", stderr)
                path.write_text(original, encoding="utf-8")
                self.assertEqual(self.run_action("check", "--require", self.report.name)[0], 0)


if __name__ == "__main__":
    unittest.main()
