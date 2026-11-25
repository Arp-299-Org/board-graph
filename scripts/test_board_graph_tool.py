#!/usr/bin/env python3
"""The board graph tool's manifest, on temp folders: a hash is the same for CR LF and LF, check names each file that
differs or is missing, manifest writes the hashes, export writes the listed files and the manifest, and update takes a
ref of the tool's repository through git, refuses local edits, deletes what the new version no longer lists, and a
second run changes nothing. The starter list is paths only, never checked, exported or updated, and `starter` writes
only what a project lacks. This repo's own manifest lists real files with their hashes."""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import board_graph_tool as tool  # noqa: E402
from no_window import KWARGS as NO_WINDOW  # noqa: E402

ROOT = HERE.parent


def run(root, *args):
    """board_graph_tool's main in `root`, with its exit code and what it printed."""
    done = subprocess.run([sys.executable, str(HERE / "board_graph_tool.py"), "--root", str(root), *args],
                          capture_output=True, text=True, check=False, **NO_WINDOW)
    return done.returncode, done.stdout + done.stderr


def git(folder, *args):
    environ = dict(os.environ, GIT_AUTHOR_NAME="test", GIT_AUTHOR_EMAIL="test@example.invalid",
                   GIT_COMMITTER_NAME="test", GIT_COMMITTER_EMAIL="test@example.invalid")
    done = subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True, check=True, env=environ,
                          **NO_WINDOW)
    return done.stdout.strip()


def write(root, files):
    for path, data in files.items():
        file = pathlib.Path(root) / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(data)


def tree(root):
    """Every file under `root` but .git, with its bytes."""
    root = pathlib.Path(root)
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*")
            if path.is_file() and ".git" not in path.relative_to(root).parts}


class Hashes(unittest.TestCase):
    def test_crlf_and_lf_hash_the_same(self):
        self.assertEqual(tool.digest(b"a\r\nb\r\n"), tool.digest(b"a\nb\n"))
        self.assertNotEqual(tool.digest(b"a\nb\n"), tool.digest(b"a\nc\n"))

    def test_a_path_must_be_relative_with_forward_slashes(self):
        for bad in ("/abs.py", "C:/x.py", "a\\b.py", "../up.py", "a/./b.py", tool.MANIFEST):
            with self.assertRaises(tool.ToolError, msg=bad):
                tool.validate({"name": "n", "repository": "r", "version": "1", "files": {bad: "0" * 64}})


class Project(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name) / "project"
        write(self.root, {"scripts/a.py": b"print('a')\r\n", "docs/b.md": b"# b\n", "own.txt": b"project's own\n"})
        code, out = run(self.root, "manifest", "--version", "0.1.0", "--add", "scripts/a.py", "docs/b.md")
        self.assertEqual(code, 0, out)

    def tearDown(self):
        self.temp.cleanup()

    def test_manifest_lists_each_file_with_its_lf_hash(self):
        data = json.loads((self.root / tool.MANIFEST).read_text(encoding="utf-8"))
        self.assertEqual((data["name"], data["repository"], data["version"]),
                         ("board-graph", "Arp-299-Org/board-graph", "0.1.0"))
        self.assertEqual(data["files"], {"docs/b.md": tool.digest(b"# b\n"),
                                         "scripts/a.py": tool.digest(b"print('a')\n")})

    def test_check_passes_and_names_each_edit_and_missing_file(self):
        self.assertEqual(run(self.root, "check")[0], 0)
        (self.root / "scripts/a.py").write_bytes(b"print('a')\n")  # LF instead of CR LF is no edit
        self.assertEqual(run(self.root, "check")[0], 0)
        (self.root / "scripts/a.py").write_bytes(b"print('edited')\n")
        (self.root / "docs/b.md").unlink()
        code, out = run(self.root, "check")
        self.assertEqual(code, 1)
        self.assertIn("differs from board-graph 0.1.0: scripts/a.py", out)
        self.assertIn("missing: docs/b.md", out)
        self.assertIn("update --from", out)
        self.assertEqual(len(out.strip().splitlines()), 3, out)

    def test_manifest_refuses_a_listed_file_that_is_missing(self):
        (self.root / "docs/b.md").unlink()
        code, out = run(self.root, "manifest", "--version", "0.2.0")
        self.assertEqual(code, 1)
        self.assertIn("docs/b.md", out)

    def test_manifest_removes_a_file_from_the_list(self):
        self.assertEqual(run(self.root, "manifest", "--version", "0.2.0", "--remove", "docs/b.md")[0], 0)
        self.assertEqual(list(tool.load(self.root)["files"]), ["scripts/a.py"])

    def test_export_writes_the_listed_files_and_the_manifest_with_lf(self):
        out = pathlib.Path(self.temp.name) / "out"
        self.assertEqual(run(self.root, "export", "--to", str(out))[0], 0)
        self.assertEqual(tree(out), {"scripts/a.py": b"print('a')\n", "docs/b.md": b"# b\n",
                                     tool.MANIFEST: (self.root / tool.MANIFEST).read_bytes()})
        self.assertEqual(run(out, "check")[0], 0)

    def test_export_keeps_a_different_file_already_at_a_tool_path_until_replace_is_given(self):
        out = pathlib.Path(self.temp.name) / "out"
        write(out, {"scripts/a.py": b"print('the project wrote this')\n"})
        code, said = run(self.root, "export", "--to", str(out))
        self.assertEqual(code, 1)
        self.assertIn("scripts/a.py", said)
        self.assertEqual(tree(out), {"scripts/a.py": b"print('the project wrote this')\n"})
        self.assertEqual(run(self.root, "export", "--to", str(out), "--replace")[0], 0)
        self.assertEqual((out / "scripts/a.py").read_bytes(), b"print('a')\n")
        self.assertEqual(run(out, "check")[0], 0)

    def test_export_refuses_an_edited_copy(self):
        (self.root / "docs/b.md").write_bytes(b"# edited\n")
        code, out = run(self.root, "export", "--to", str(pathlib.Path(self.temp.name) / "out"))
        self.assertEqual(code, 1)
        self.assertIn("docs/b.md", out)


class Update(unittest.TestCase):
    """The tool's repository at 0.1.0 and 0.2.0, and a project pinned at 0.1.0."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.temp.name)
        self.tool, self.project = base / "tool", base / "project"
        write(self.tool, {"scripts/a.py": b"a = 1\n", "scripts/gone.py": b"gone\n", "docs/b.md": b"b\n"})
        self.assertEqual(run(self.tool, "manifest", "--version", "0.1.0", "--add", "scripts/a.py",
                             "scripts/gone.py", "docs/b.md")[0], 0)
        git(self.tool, "init", "-q")
        git(self.tool, "add", "-A")
        git(self.tool, "commit", "-q", "-m", "0.1.0")
        git(self.tool, "tag", "v0.1.0")
        self.assertEqual(run(self.tool, "export", "--to", str(self.project))[0], 0)
        write(self.project, {"own.txt": b"project's own\n", "scripts/a.py": b"a = 1\r\n"})  # a CR LF checkout
        write(self.tool, {"scripts/a.py": b"a = 2\n", "scripts/new.py": b"new\n",
                          "UPGRADING.md": b"# Notes\n\n## 0.2.0\n\nMove the listings to stock/.\n\n"
                                          b"## 0.1.0\n\nNothing to change.\n"})
        (self.tool / "scripts/gone.py").unlink()
        self.assertEqual(run(self.tool, "manifest", "--version", "0.2.0", "--add", "scripts/new.py",
                             "--remove", "scripts/gone.py")[0], 0)
        git(self.tool, "add", "-A")
        git(self.tool, "commit", "-q", "-m", "0.2.0")
        git(self.tool, "tag", "v0.2.0")
        # Working files the tool's checkout has not committed are not the release.
        write(self.tool, {"scripts/a.py": b"uncommitted\n"})

    def tearDown(self):
        self.temp.cleanup()

    def test_update_takes_the_ref_through_git_and_a_second_run_changes_nothing(self):
        code, out = run(self.project, "update", "--from", str(self.tool), "--ref", "v0.2.0")
        self.assertEqual(code, 0, out)
        files = tree(self.project)
        self.assertEqual(files["scripts/a.py"], b"a = 2\r\n")  # the copy keeps its line endings
        self.assertEqual(files["scripts/new.py"], b"new\n")
        self.assertNotIn("scripts/gone.py", files)
        self.assertEqual(files["own.txt"], b"project's own\n")
        self.assertEqual(tool.load(self.project)["version"], "0.2.0")
        self.assertEqual(run(self.project, "check")[0], 0)
        code, out = run(self.project, "update", "--from", str(self.tool), "--ref", "v0.2.0")
        self.assertEqual(code, 0, out)
        self.assertIn("0 written, 0 deleted", out)
        self.assertEqual(tree(self.project), files)

    def test_update_ends_with_what_the_project_changes_for_each_version_it_passed(self):
        code, out = run(self.project, "update", "--from", str(self.tool), "--ref", "v0.2.0")
        self.assertEqual(code, 0, out)
        self.assertIn("What this project changes in its own files, by version:\n\n0.2.0\n\n"
                      "Move the listings to stock/.", out)
        self.assertNotIn("Nothing to change.", out)  # 0.1.0 is the version it had
        code, out = run(self.project, "update", "--from", str(self.tool), "--ref", "v0.2.0")
        self.assertNotIn("What this project changes", out)  # nothing passed, nothing to say
        self.assertEqual(tool.upgrade_notes("## 0.9.0\na\n## 0.10.0\nb\n## 0.11.2\nc\n", "0.9.0", "0.11.0"),
                         [("0.10.0", "b")])  # by number, not by letter: 0.10.0 is after 0.9.0

    def test_a_version_is_not_released_without_its_notes(self):
        git(self.tool, "checkout", "-q", "--", "scripts/a.py")
        code, out = run(self.tool, "notes")
        self.assertEqual((code, out.strip()), (0, "Move the listings to stock/."))
        self.assertEqual(run(self.tool, "manifest", "--version", "0.3.0")[0], 0)
        code, out = run(self.tool, "notes")
        self.assertEqual(code, 1)
        self.assertIn("UPGRADING.md has no section for 0.3.0", out)

    def test_update_at_the_pinned_version_changes_nothing(self):
        before = tree(self.project)
        code, out = run(self.project, "update", "--from", str(self.tool), "--ref", "v0.1.0")
        self.assertEqual(code, 0, out)
        self.assertIn("0 written, 0 deleted", out)
        self.assertEqual(tree(self.project), before)

    def test_update_refuses_a_local_edit_and_changes_nothing(self):
        (self.project / "docs/b.md").write_bytes(b"edited here\n")
        before = tree(self.project)
        code, out = run(self.project, "update", "--from", str(self.tool), "--ref", "v0.2.0")
        self.assertEqual(code, 1)
        self.assertIn("docs/b.md", out)
        self.assertEqual(tree(self.project), before)

    def test_update_refuses_an_unknown_ref(self):
        before = tree(self.project)
        code, out = run(self.project, "update", "--from", str(self.tool), "--ref", "v9.9.9")
        self.assertEqual(code, 1)
        self.assertEqual(tree(self.project), before)


class Starter(unittest.TestCase):
    """The tool's repository with two tool files and three starter files, committed and tagged."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.temp.name)
        self.tool, self.project = base / "tool", base / "project"
        write(self.tool, {"scripts/a.py": b"a = 1\n", "docs/CONTRACT.md": b"contract\n",
                          "AGENTS.md": b"starter agents\n", "tools/graph.json": b"{}\n", ".agents/x/SKILL.md": b"skill\n"})
        code, out = run(self.tool, "manifest", "--version", "0.1.0", "--add", "scripts/a.py", "docs/CONTRACT.md",
                        "--add-starter", "AGENTS.md", "tools/graph.json", ".agents/x/SKILL.md")
        self.assertEqual(code, 0, out)
        git(self.tool, "init", "-q")
        git(self.tool, "add", "-A")
        git(self.tool, "commit", "-q", "-m", "0.1.0")
        git(self.tool, "tag", "v0.1.0")

    def tearDown(self):
        self.temp.cleanup()

    def test_manifest_keeps_the_starter_list_without_hashes(self):
        data = json.loads((self.tool / tool.MANIFEST).read_text(encoding="utf-8"))
        self.assertEqual(data["starter"], [".agents/x/SKILL.md", "AGENTS.md", "tools/graph.json"])
        self.assertEqual(sorted(data["files"]), ["docs/CONTRACT.md", "scripts/a.py"])
        # Another manifest run keeps the list; --remove-starter and --add-starter change it.
        self.assertEqual(run(self.tool, "manifest", "--version", "0.1.1")[0], 0)
        self.assertEqual(tool.load(self.tool)["starter"], data["starter"])
        self.assertEqual(run(self.tool, "manifest", "--version", "0.1.2", "--remove-starter", "tools/graph.json")[0], 0)
        self.assertEqual(tool.load(self.tool)["starter"], [".agents/x/SKILL.md", "AGENTS.md"])
        code, out = run(self.tool, "manifest", "--version", "0.1.3", "--remove-starter", "tools/graph.json")
        self.assertEqual(code, 1, out)
        self.assertIn("tools/graph.json is not in the starter list", out)

    def test_a_path_cannot_be_in_both_lists(self):
        before = (self.tool / tool.MANIFEST).read_bytes()
        for args in (("--add-starter", "scripts/a.py"), ("--add", "AGENTS.md")):
            code, out = run(self.tool, "manifest", "--version", "0.2.0", *args)
            self.assertEqual(code, 1, out)
            self.assertIn("never both", out)
        self.assertEqual((self.tool / tool.MANIFEST).read_bytes(), before)
        with self.assertRaises(tool.ToolError):
            tool.validate({"name": "n", "repository": "r", "version": "1", "files": {"a": "0" * 64}, "starter": ["a"]})

    def test_check_export_and_update_leave_the_starter_files_alone(self):
        (self.tool / "AGENTS.md").write_bytes(b"edited starter\n")
        (self.tool / "tools/graph.json").unlink()
        self.assertEqual(run(self.tool, "check")[0], 0)
        self.assertEqual(run(self.tool, "export", "--to", str(self.project))[0], 0)
        self.assertEqual(sorted(tree(self.project)), sorted(["docs/CONTRACT.md", tool.MANIFEST, "scripts/a.py"]))
        code, out = run(self.project, "update", "--from", str(self.tool), "--ref", "v0.1.0")
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(tree(self.project)), sorted(["docs/CONTRACT.md", tool.MANIFEST, "scripts/a.py"]))

    def test_starter_writes_what_the_project_lacks_from_git_and_a_second_run_writes_nothing(self):
        (self.tool / "AGENTS.md").write_bytes(b"uncommitted\n")  # the checkout's working files are not the release
        code, out = run(self.project, "starter", "--from", str(self.tool), "--ref", "v0.1.0")
        self.assertEqual(code, 0, out)
        self.assertIn("3 written, 0 skipped", out)
        self.assertEqual(tree(self.project), {"AGENTS.md": b"starter agents\n", "tools/graph.json": b"{}\n",
                                              ".agents/x/SKILL.md": b"skill\n"})
        code, again = run(self.project, "starter", "--from", str(self.tool), "--ref", "v0.1.0")
        self.assertEqual(code, 0, again)
        self.assertIn("0 written, 3 skipped", again)
        self.assertEqual(len(tree(self.project)), 3)

    def test_starter_never_overwrites_a_file_the_project_has_and_says_so(self):
        write(self.project, {"AGENTS.md": b"the project's own\r\n"})
        code, out = run(self.project, "starter", "--from", str(self.tool), "--ref", "v0.1.0")
        self.assertEqual(code, 0, out)
        self.assertIn("2 written, 1 skipped", out)
        self.assertIn("  skipped AGENTS.md: this project has it", out)
        self.assertIn("  wrote tools/graph.json", out)
        self.assertEqual((self.project / "AGENTS.md").read_bytes(), b"the project's own\r\n")

    def test_starter_leaves_out_what_skip_names_and_refuses_a_path_that_is_not_a_starter_file(self):
        code, out = run(self.project, "starter", "--from", str(self.tool), "--ref", "v0.1.0", "--skip", "AGENTS.md")
        self.assertEqual(code, 0, out)
        self.assertIn("2 written, 0 skipped because this project has them, 1 left out by --skip", out)
        self.assertIn("  left out AGENTS.md: --skip named it", out)
        self.assertEqual(sorted(tree(self.project)), [".agents/x/SKILL.md", "tools/graph.json"])
        code, again = run(self.project, "starter", "--from", str(self.tool), "--ref", "v0.1.0", "--skip", "AGENTS.md")
        self.assertEqual(code, 0, again)
        self.assertIn("0 written, 2 skipped because this project has them, 1 left out by --skip", again)
        self.assertFalse((self.project / "AGENTS.md").exists())
        code, out = run(self.project, "starter", "--from", str(self.tool), "--ref", "v0.1.0",
                        "--skip", "docs/NOT_A_STARTER.md")
        self.assertEqual(code, 1, out)
        self.assertIn("--skip names what is not a starter file of board-graph 0.1.0: docs/NOT_A_STARTER.md", out)
        self.assertFalse((self.project / "AGENTS.md").exists())

    def test_starter_refuses_an_unknown_ref_and_writes_nothing(self):
        code, out = run(self.project, "starter", "--from", str(self.tool), "--ref", "v9.9.9")
        self.assertEqual(code, 1, out)
        self.assertFalse(self.project.exists())

    def test_update_keeps_a_file_the_new_version_makes_a_starter_file(self):
        self.assertEqual(run(self.tool, "export", "--to", str(self.project))[0], 0)
        code, out = run(self.tool, "manifest", "--version", "0.2.0", "--remove", "docs/CONTRACT.md",
                        "--add-starter", "docs/CONTRACT.md")
        self.assertEqual(code, 0, out)
        git(self.tool, "commit", "-q", "-am", "0.2.0")
        code, out = run(self.project, "update", "--from", str(self.tool), "--ref", "HEAD")
        self.assertEqual(code, 0, out)
        self.assertEqual((self.project / "docs/CONTRACT.md").read_bytes(), b"contract\n")
        self.assertIn("0 deleted", out)


class ToolManifest(unittest.TestCase):
    def test_the_manifest_is_valid_and_lists_this_script_and_its_test(self):
        data = tool.load(ROOT)
        self.assertEqual((data["name"], data["repository"]), ("board-graph", "Arp-299-Org/board-graph"))
        for path in ("scripts/board_graph_tool.py", "scripts/test_board_graph_tool.py",
                     "docs/BOARD_PIPELINE_CONTRACT.md"):
            self.assertIn(path, data["files"])
        self.assertNotIn("tools/board_graph.json", data["files"])
        self.assertIn("tools/board_graph.json", data.get("starter", []))


if __name__ == "__main__":
    unittest.main()
