"""fresh_checkout: which files an edit names, from Claude Code's hook input and from Codex's."""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fresh_checkout  # noqa: E402

PATCH = """*** Begin Patch
*** Update File: scripts/tidy.py
@@
-a
+b
*** Add File: docs/new.md
+hello
*** Delete File: old.txt
*** Update File: /elsewhere/x.py
*** Move to: /elsewhere/y.py
*** End Patch
"""


class TargetTests(unittest.TestCase):
    def test_claude_names_one_file(self):
        self.assertEqual(fresh_checkout.targets({"tool_name": "Edit", "tool_input": {"file_path": "/r/a.py"}}),
                         [pathlib.Path("/r/a.py")])
        self.assertEqual(fresh_checkout.targets({"tool_input": {"notebook_path": "/r/n.ipynb"}}),
                         [pathlib.Path("/r/n.ipynb")])

    def test_codex_patch_names_its_files_relative_to_the_sessions_cwd(self):
        event = {"tool_name": "apply_patch", "cwd": "/repo", "tool_input": {"command": PATCH}}
        self.assertEqual(fresh_checkout.targets(event),
                         [pathlib.Path("/repo/scripts/tidy.py"), pathlib.Path("/repo/docs/new.md"),
                          pathlib.Path("/repo/old.txt"), pathlib.Path("/elsewhere/x.py"),
                          pathlib.Path("/elsewhere/y.py")])

    def test_nothing_named_means_nothing_to_check(self):
        self.assertEqual(fresh_checkout.targets({"tool_name": "Bash", "tool_input": {"command": "ls"}}), [])
        self.assertEqual(fresh_checkout.targets({}), [])


if __name__ == "__main__":
    unittest.main()
