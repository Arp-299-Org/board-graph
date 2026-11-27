#!/usr/bin/env python3
"""Unit and lifecycle tests for board-graph's repo-local dev workflow under dev/."""
from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock as mock

DEV_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = DEV_DIR.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(DEV_DIR) not in sys.path:
    sys.path.insert(0, str(DEV_DIR))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import board_graph_tool  # noqa: E402
import tool_actions  # noqa: E402


def _set_pyproject_version(root: pathlib.Path, version: str) -> None:
    pyproject = root / "pyproject.toml"
    updated, count = re.subn(
        r'(?m)^version\s*=\s*"[^"]+"',
        f'version = "{version}"',
        pyproject.read_text(encoding="utf-8"),
        count=1,
    )
    assert count == 1, "version line not found in pyproject.toml"
    pyproject.write_text(updated, encoding="utf-8")


def _trim_temp_repo_tests(root: pathlib.Path, keep: tuple[str, ...] = ("test_site_settings.py",)) -> None:
    """Drop unrelated unit test files in temp copies so unstubbed run_tool_checks() finishes quickly."""
    removed: set[str] = set()
    for folder in ("scripts", "scripts/hooks", "tools/kicad"):
        base = root / folder
        if not base.is_dir():
            continue
        for test_file in base.glob("test_*.py"):
            if test_file.name not in keep:
                removed.add(test_file.relative_to(root).as_posix())
                test_file.unlink()
    manifest_path = root / "tools" / "board_graph_tool.json"
    if removed and manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        files = manifest.get("files", {})
        for rel in removed:
            files.pop(rel, None)
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def _init_temp_repo(tmp: pathlib.Path, *, git: bool = True, trim_tests: bool = False) -> pathlib.Path:
    """Create a complete disposable copy of board-graph's files for isolated dev graph tests."""
    root = tmp / "repo"
    for folder in (".agents", ".claude", ".codex", ".github", "board", "dev", "docs", "scripts", "tools"):
        src = REPO_ROOT / folder
        if src.is_dir():
            shutil.copytree(src, root / folder, ignore=shutil.ignore_patterns("__pycache__", "logs"))
    for fname in ("AGENTS.md", "CLAUDE.md", "pyproject.toml", "UPGRADING.md", ".gitignore"):
        src_file = REPO_ROOT / fname
        if src_file.is_file():
            shutil.copyfile(src_file, root / fname)
    if trim_tests:
        _trim_temp_repo_tests(root)

    if git:
        for cmd in (
            ["git", "init", "-b", "main"],
            ["git", "config", "user.name", "Test Bot"],
            ["git", "config", "user.email", "test@example.com"],
            ["git", "add", "."],
            ["git", "commit", "-m", "Initial snapshot"],
            ["git", "tag", "-a", "v0.28.1", "-m", "Release 0.28.1"],
        ):
            subprocess.run(cmd, cwd=str(root), check=True, capture_output=True, text=True)
    return root


class ToolDevWorkflowTests(unittest.TestCase):
    def test_dev_graph_validates_and_never_leaks_into_manifest_or_starter(self) -> None:
        manifest = json.loads((REPO_ROOT / "tools" / "board_graph_tool.json").read_text(encoding="utf-8"))
        leaked = [p for p in (*manifest.get("files", {}), *manifest.get("starter", [])) if p.startswith("dev/")]
        self.assertEqual(leaked, [], f"dev/ files must not appear in board_graph_tool.json: {leaked}")

        starter_graph = json.loads((REPO_ROOT / "tools" / "board_graph.json").read_text(encoding="utf-8"))
        self.assertEqual([t["id"] for t in starter_graph["tracks"]], ["board", "framework"])

        env = dict(os.environ)
        env["BOARD_GRAPH_PACK"] = str(REPO_ROOT / "dev" / "tool_pack.json")
        done = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "board_graph.py"), "--graph", str(REPO_ROOT / "dev" / "tool_graph.json"), "check"],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)

    def test_tool_code_fingerprint_pins_code_and_ignores_release_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _init_temp_repo(pathlib.Path(td), git=False)

            fp0 = tool_actions.tool_code_fingerprint(root)
            # Bumping pyproject.toml, UPGRADING.md, and manifest version preserves tool_code fingerprint
            _set_pyproject_version(root, "0.29.0")
            (root / "UPGRADING.md").write_text(
                "## 0.29.0\n\nNothing changes.\n\n" + (root / "UPGRADING.md").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            board_graph_tool.manifest(root, "0.29.0")
            fp_meta = tool_actions.tool_code_fingerprint(root)
            self.assertEqual(fp0["digest"], fp_meta["digest"])

            # Modifying any script invalidates the tool_code fingerprint
            sample = root / "scripts" / "kicad_rules_geom.py"
            sample.write_text(sample.read_text(encoding="utf-8") + "\n# change\n", encoding="utf-8")
            fp_code = tool_actions.tool_code_fingerprint(root)
            self.assertNotEqual(fp0["digest"], fp_code["digest"])

    def test_live_smoke_refuses_busy_mac_and_skips_when_no_ipc_changed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            with mock.patch.object(tool_actions, "check_mac_idle", return_value=(False, "4321 pcbnew demo-board")):
                res = tool_actions.run_live_smoke(root)
                self.assertEqual(res["outcome"], "needs_lead")
                self.assertIn("not idle", res["findings"][0])

            with (
                mock.patch.object(tool_actions, "check_mac_idle", return_value=(True, "NONE_RUNNING")),
                mock.patch.object(tool_actions, "changed_ipc_routing_files", return_value=("v0.28.1", [])),
            ):
                res = tool_actions.run_live_smoke(root)
                self.assertEqual(res["outcome"], "pass")
                self.assertFalse(res["detail"]["ipc_changed"])

    def test_enforce_lifecycle_recheck_release_gate_and_next_release_cycling(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _init_temp_repo(pathlib.Path(td), trim_tests=True)
            env = dict(os.environ)
            env["BOARD_GRAPH_PROJECT"] = str(root)
            env["BOARD_GRAPH_DEV_IN_TOOL_CHECKS"] = "1"
            for k in ("BOARD_GRAPH_STATE", "BOARD_GRAPH_RECORDS", "BOARD_GRAPH_VIEW", "BOARD_GRAPH_BOARD"):
                env.pop(k, None)

            def dev(*args: str, expected: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess:
                res = subprocess.run(
                    [sys.executable, str(root / "dev" / "tool_dev.py"), *args],
                    cwd=str(root),
                    env=env,
                    capture_output=True,
                    text=True,
                )
                if expected is not None and res.returncode not in expected:
                    self.fail(f"dev{args} returned {res.returncode}, expected {expected}\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}")
                return res

            def start_card(note: str, by: str = "agent:lead") -> str:
                out = dev("next", "--by", by, "--note", note, expected=(2,)).stdout
                where_now = json.loads(dev("where", "--json").stdout)
                working = where_now["tracks"][0]["node"]["working"]
                self.assertEqual(len(working), 1, out)
                return working[0]["run_id"]

            # Start v0.29.0 cycle and complete triage -> implement -> unstubbed tool_checks -> cold_review -> live_smoke
            dev("cycle", "v0.29.0")
            where0 = json.loads(dev("where", "--json").stdout)
            self.assertEqual(where0["tracks"][0]["position"], "triage")

            att_triage = start_card("Start triage")
            ev = root / "logs" / "tool-dev" / "releases" / "v0.29.0" / "notes.md"
            ev.write_text("# Notes\nAll checks verified.\n", encoding="utf-8")
            ev_rel = "logs/tool-dev/releases/v0.29.0/notes.md"

            dev("record", "triage", "pass", "--attempt", att_triage, "--by", "agent:lead", "--note", "Triaged", "--evidence", ev_rel)
            att_impl = start_card("Start implement")
            impl_file = root / "scripts" / "kicad_rules_geom.py"
            impl_file.write_text(impl_file.read_text(encoding="utf-8") + "\n# implementation change\n", encoding="utf-8")
            subprocess.run(["git", "add", "scripts/kicad_rules_geom.py"], cwd=str(root), check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "Implement tool change before release metadata bump"], cwd=str(root), check=True, capture_output=True)
            dev("record", "implement", "pass", "--attempt", att_impl, "--by", "agent:lead", "--note", "Implemented", "--evidence", ev_rel)

            # Stub only live_smoke (no Mac SSH in unit tests); keep real run_tool_checks() active!
            actions_file = root / "dev" / "tool_actions.py"
            orig_actions = actions_file.read_text(encoding="utf-8")
            patched = orig_actions + (
                "\ndef _fast_pass(r):\n"
                "    return {'outcome': 'pass', 'findings': [], 'detail': {'stub': True}}\n"
                "ACTIONS['live_smoke'] = (_fast_pass, ACTIONS['live_smoke'][1])\n"
            )
            actions_file.write_text(patched, encoding="utf-8")
            subprocess.run(["git", "add", "dev/tool_actions.py"], cwd=str(root), check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "Stub live_smoke only"], cwd=str(root), check=True, capture_output=True)

            # Real tool_checks (auto) runs unstubbed before pyproject/UPGRADING/manifest are bumped and passes (exit 0)
            dev("next")
            att_rev = start_card("Cold review", by="agent:reviewer")
            dev("record", "cold_review", "pass", "--attempt", att_rev, "--by", "agent:reviewer", "--note", "Clean diff", "--evidence", ev_rel)
            att_smoke = start_card("Live smoke")
            dev("record", "live_smoke", "pass", "--attempt", att_smoke, "--by", "agent:lead", "--note", "Smoke ok", "--evidence", ev_rel)
            att_rel = start_card("Ready to release")

            # Modifying a script invalidates upstream approvals and blocks release pass
            target_py = root / "scripts" / "kicad_rules_geom.py"
            saved_py = target_py.read_bytes()
            target_py.write_bytes(saved_py + b"\n# drift\n")
            bad_rel = dev("record", "release", "pass", "--attempt", att_rel, "--by", "agent:lead", "--note", "Should fail", "--evidence", ev_rel, expected=(1,))
            self.assertNotEqual(bad_rel.returncode, 0)
            target_py.write_bytes(saved_py)

            # Switching away before done is refused unless --force is given
            dev("cycle", "v0.29.1", expected=(1,))

            # Bump, update UPGRADING.md, manifest, commit (WITHOUT tag yet) -> release_gate refuses missing tag!
            _set_pyproject_version(root, "0.29.0")
            (root / "UPGRADING.md").write_text(
                "# What\n\n## 0.29.0\n\nTest release.\n\n## 0.28.1\n\nPrev.\n",
                encoding="utf-8",
            )
            subprocess.run(
                [sys.executable, str(root / "scripts" / "board_graph_tool.py"), "manifest", "--version", "0.29.0"],
                cwd=str(root),
                check=True,
                capture_output=True,
            )
            subprocess.run(["git", "add", "pyproject.toml", "UPGRADING.md", "tools/board_graph_tool.json"], cwd=str(root), check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "Release 0.29.0"], cwd=str(root), check=True, capture_output=True)

            no_tag = dev("record", "release", "pass", "--attempt", att_rel, "--by", "agent:lead", "--note", "Missing tag", "--evidence", ev_rel, expected=(1,))
            self.assertIn("annotated tag v0.29.0 is missing", no_tag.stderr + no_tag.stdout)

            # Now create annotated tag v0.29.0 -> approvals stay current and release_gate passes!
            subprocess.run(["git", "tag", "-a", "v0.29.0", "-m", "Release 0.29.0"], cwd=str(root), check=True, capture_output=True)

            dev("record", "release", "pass", "--attempt", att_rel, "--by", "agent:lead", "--note", "Released v0.29.0", "--evidence", ev_rel)
            where_done = json.loads(dev("where", "--json").stdout)
            self.assertEqual(where_done["tracks"][0]["position"], "done")

            # Cycling to v0.29.1 and v0.30.0 works seamlessly with zero manual state surgery
            dev("cycle", "v0.29.1")
            where_next = json.loads(dev("where", "--json").stdout)
            self.assertEqual(where_next["tracks"][0]["position"], "triage")
            cycles_out = dev("cycles").stdout
            self.assertIn("v0.29.0: done", cycles_out)
            self.assertIn("* v0.29.1: triage", cycles_out)

    def test_tool_checks_fail_fast_on_hygiene_and_records_timings(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _init_temp_repo(pathlib.Path(td), git=False, trim_tests=True)
            target = root / "scripts" / "kicad_rules_geom.py"
            orig = target.read_text(encoding="utf-8")
            with mock.patch.dict(os.environ, {"BOARD_GRAPH_DEV_IN_TOOL_CHECKS": "1"}):
                try:
                    target.write_text(orig + "\n# TODO: trigger fail-fast preflight\n", encoding="utf-8")
                    bad = tool_actions.run_tool_checks(root)
                    self.assertEqual(bad["outcome"], "needs_lead")
                    self.assertTrue(bad["detail"]["preflight_failed"])
                    self.assertGreaterEqual(bad["detail"]["skipped_suites"], 2)
                    self.assertIn("hygiene", bad["detail"]["failed"])
                finally:
                    target.write_text(orig, encoding="utf-8")

                good = tool_actions.run_tool_checks(root)
            self.assertEqual(good["outcome"], "pass", good["findings"])
            self.assertFalse(good["detail"]["preflight_failed"])
            self.assertFalse(good["detail"]["fail_fast_aborted"])
            self.assertEqual(good["detail"]["skipped_suites"], 0)
            self.assertIn("elapsed_s", good["detail"])
            self.assertIn("timings", good["detail"])
            self.assertGreaterEqual(len(good["detail"]["slowest"]), 1)

    def test_tool_checks_fail_fast_aborts_remaining_suites_on_first_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _init_temp_repo(pathlib.Path(td), git=False, trim_tests=True)
            fast_fail = root / "scripts" / "test_aaa_fail.py"
            fast_fail.write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
            slow_suite = root / "scripts" / "test_zzz_slow.py"
            slow_suite.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
            board_graph_tool.manifest(
                root,
                "0.33.1",
                add=("scripts/test_aaa_fail.py", "scripts/test_zzz_slow.py"),
            )
            with mock.patch.dict(os.environ, {"BOARD_GRAPH_DEV_IN_TOOL_CHECKS": "1"}):
                t0 = __import__("time").monotonic()
                res = tool_actions.run_tool_checks(root, fail_fast=True)
                elapsed = __import__("time").monotonic() - t0
            self.assertEqual(res["outcome"], "needs_lead")
            self.assertFalse(res["detail"]["preflight_failed"])
            self.assertTrue(res["detail"]["fail_fast_aborted"])
            self.assertGreaterEqual(res["detail"]["skipped_suites"], 1)
            self.assertIn("scripts/test_aaa_fail.py", res["detail"]["failed"])
            self.assertLess(elapsed, 15.0)


if __name__ == "__main__":
    unittest.main()
