#!/usr/bin/env python3
"""A project's own card scripts (board_graph_actions.project_actions): read from the project's file, run by next
like the tool's own, and refused when the file is not as documented or takes a name the tool has."""
import io
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import board_graph_actions  # noqa: E402
from board_graph_actions import project_actions  # noqa: E402

GOOD = """
LIMIT_MS = 2

def build(root):
    \"\"\"The firmware builds.\"\"\"
    return {"outcome": "pass", "findings": [], "detail": {"root": str(root)}}

def tests(root):
    return {"outcome": "needs_lead", "findings": ["one test fails"], "detail": {}}

ACTIONS = {"firmware_build": (build, ["firmware/Makefile"]), "firmware_tests": (tests, ())}
"""


class ProjectActions(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = pathlib.Path(folder.name) / "board_graph_project_actions.py"

    def write(self, text):
        self.path.write_text(text, encoding="utf-8")
        return self.path

    def test_a_project_without_the_file_has_none(self):
        self.assertEqual(project_actions(self.path), {})

    def test_the_files_actions_are_read_with_what_they_read(self):
        found = project_actions(self.write(GOOD), ("baseline",))
        self.assertEqual(sorted(found), ["firmware_build", "firmware_tests"])
        self.assertEqual(found["firmware_build"].reads, ("firmware/Makefile",))
        self.assertEqual(found["firmware_build"].run("here")["detail"], {"root": "here"})
        self.assertEqual(found["firmware_tests"].run("here")["findings"], ["one test fails"])

    def test_a_name_the_tool_has_is_refused(self):
        with self.assertRaisesRegex(ValueError, "names the action baseline, which the board graph tool has"):
            project_actions(self.write(GOOD.replace("firmware_build", "baseline")), ("baseline",))

    def test_a_file_that_is_not_as_documented_is_refused(self):
        for text, said in (("X = 1\n", "does not set ACTIONS"),
                           ("ACTIONS = {'a': 3}\n", "the action a is not"),
                           ("ACTIONS = {'a': (print, 'one/path')}\n", "what the action a reads"),
                           ("ACTIONS = {'': (print, [])}\n", "not a name")):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, said):
                project_actions(self.write(text))

    def test_next_runs_a_projects_script_like_the_tools_own(self):
        # run_script takes any (function, reads): a project's action is run, checked and reported the same way.
        import board_graph
        found = project_actions(self.write(GOOD))
        self.assertEqual(board_graph.checked(found["firmware_tests"].run("."))["outcome"], "needs_lead")

    def test_the_tools_own_names_are_what_a_project_cannot_take(self):
        for name in ("baseline", "erc", "routing_run"):
            self.assertIn(name, (*board_graph_actions.ACTIONS, *board_graph_actions.LATER,
                                 *board_graph_actions.COMMANDS))


if __name__ == "__main__":
    unittest.main()
