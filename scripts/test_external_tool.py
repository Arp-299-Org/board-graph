#!/usr/bin/env python3
"""External shared board-graph checkout tests: tool_paths, CLI launcher, unvendor, serve, and routing."""
from __future__ import annotations

import copy
import hashlib
import http.client
import json
import os
import pathlib
import plistlib
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
FIXTURES = HERE / "fixtures"
os.environ.setdefault("BOARD_GRAPH_BOARD", str(FIXTURES / "board.json"))
os.environ.setdefault("BOARD_GRAPH_SITE", str(FIXTURES / "board_graph_site.json"))
os.environ.setdefault("BOARD_GRAPH_BOARDS", str(FIXTURES / "no_boards.json"))

import board_approvals  # noqa: E402
import board_graph_cli  # noqa: E402
import board_graph_serve  # noqa: E402
import board_graph_tool  # noqa: E402
import editor_step  # noqa: E402
import routing_codex  # noqa: E402
import routing_gemini  # noqa: E402
import routing_run  # noqa: E402
import site_settings  # noqa: E402
import test_site_settings  # noqa: E402
import tool_paths  # noqa: E402

TOOL_ROOT = HERE.parent


class ToolPathsTests(unittest.TestCase):
    def test_project_root_and_is_external_follow_environ(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = pathlib.Path(tmp).resolve()
            self.assertEqual(tool_paths.project_root({}), tool_paths.TOOL_ROOT)
            self.assertFalse(tool_paths.is_external({}))
            self.assertFalse(tool_paths.is_external({tool_paths.ENV: str(tool_paths.TOOL_ROOT)}))
            env = {tool_paths.ENV: str(project)}
            self.assertEqual(tool_paths.project_root(env), project)
            self.assertTrue(tool_paths.is_external(env))

    def test_take_argument_extracts_project_and_sets_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = pathlib.Path(tmp).resolve()
            argv = ["--project", str(project), "where", "--", "--project", "ignored"]
            env = {}
            found = tool_paths.take_argument(argv, environ=env)
            self.assertEqual(found, project)
            self.assertEqual(env[tool_paths.ENV], str(project))
            self.assertEqual(argv, ["where", "--", "--project", "ignored"])

            argv_eq = [f"--project={project}", "check"]
            env_eq = {}
            self.assertEqual(tool_paths.take_argument(argv_eq, environ=env_eq), project)
            self.assertEqual(argv_eq, ["check"])

            missing = pathlib.Path(tmp) / "does-not-exist"
            with self.assertRaises(SystemExit):
                tool_paths.take_argument(["--project", str(missing), "where"], environ={})

    def test_find_project_root_walks_up_and_stops_at_git_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp).resolve()
            proj = base / "proj"
            sub = proj / "board" / "nested"
            sub.mkdir(parents=True)
            (proj / "tools").mkdir()
            (proj / "tools" / "board_graph.json").write_text("{}", encoding="utf-8")
            self.assertEqual(tool_paths.find_project_root(sub, environ={}), proj)

            bare_git = base / "unrelated"
            deep = bare_git / "a" / "b"
            deep.mkdir(parents=True)
            (bare_git / ".git").mkdir()
            with self.assertRaises(ValueError):
                tool_paths.find_project_root(deep, environ={})

    def test_tool_fingerprint_hashes_local_or_external_manifest_and_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = pathlib.Path(tmp).resolve()
            (proj / "tools").mkdir()
            self.assertIsNone(tool_paths.tool_fingerprint(proj, environ={}))
            with mock.patch.object(tool_paths, "tool_commit", return_value="a" * 40):
                fp_a = tool_paths.tool_fingerprint(proj, environ={tool_paths.ENV: str(proj)})
            with mock.patch.object(tool_paths, "tool_commit", return_value="b" * 40):
                fp_b = tool_paths.tool_fingerprint(proj, environ={tool_paths.ENV: str(proj)})
            self.assertIsNotNone(fp_a)
            self.assertNotEqual(fp_a, fp_b)
            local = proj / "tools" / "board_graph_tool.json"
            local.write_text('{"name": "board-graph"}', encoding="utf-8")
            expected = hashlib.sha256(local.read_bytes()).hexdigest()
            self.assertEqual(tool_paths.tool_fingerprint(proj, environ={tool_paths.ENV: str(proj)}), expected)


class SiteSettingsToolCheckoutTests(unittest.TestCase):
    def test_optional_tool_checkout_validates_and_loads(self):
        data = copy.deepcopy(test_site_settings.OTHER)
        self.assertIsNone(site_settings.Settings(site_settings.validate(data), "site.json").tool_checkout)
        data["kicad_machine"]["tool_checkout"] = "~/board-graph"
        loaded = site_settings.Settings(site_settings.validate(data), "site.json")
        self.assertEqual(loaded.tool_checkout, "~/board-graph")
        bad = copy.deepcopy(data)
        bad["kicad_machine"]["tool_checkout"] = ""
        with self.assertRaisesRegex(site_settings.SiteError, r"kicad_machine\.tool_checkout must be text"):
            site_settings.validate(bad)


class UnvendorTests(unittest.TestCase):
    def _make_vendored_project(self, root: pathlib.Path, *, one_machine=False):
        (root / "scripts").mkdir(parents=True)
        (root / "tools" / "kicad").mkdir(parents=True)
        (root / "tools" / "board_graph_view").mkdir(parents=True)
        (root / "tools" / "board_graph_records").mkdir(parents=True)
        (root / "scripts" / "a.py").write_bytes(b"print('a')\n")
        (root / "tools" / "kicad" / "k.py").write_bytes(b"print('k')\n")
        (root / "tools" / "board_graph_view" / "index.html").write_bytes(b"<html></html>\n")
        (root / "tools" / "board_graph_view" / ".gitignore").write_bytes(b"view.json\n")
        (root / "scripts" / "board_graph_project_actions.py").write_bytes(b"ACTIONS = {}\n")
        (root / "tools" / "board_graph.json").write_text('{"nodes": []}\n', encoding="utf-8")
        (root / "AGENTS.md").write_text("# agents\n", encoding="utf-8")
        site = {"one_machine": True, "state_database": "db.sqlite3"} if one_machine else copy.deepcopy(test_site_settings.OTHER)
        (root / "tools" / "board_graph_site.json").write_text(json.dumps(site), encoding="utf-8")
        files = {
            "scripts/a.py": board_graph_tool.digest(b"print('a')\n"),
            "tools/kicad/k.py": board_graph_tool.digest(b"print('k')\n"),
            "tools/board_graph_view/index.html": board_graph_tool.digest(b"<html></html>\n"),
            "tools/board_graph_view/.gitignore": board_graph_tool.digest(b"view.json\n"),
        }
        manifest = {
            "name": "board-graph",
            "repository": "Arp-299-Org/board-graph",
            "version": "0.19.0",
            "files": files,
            "starter": ["AGENTS.md", "tools/board_graph.json", "tools/board_graph_site.json"],
        }
        (root / "tools" / "board_graph_tool.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    def test_unvendor_dry_run_refuses_edits_and_preserves_project_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self._make_vendored_project(root)

            # Dry run keeps everything.
            self.assertEqual(board_graph_tool.unvendor(root, mac_tool_checkout="~/board-graph", dry_run=True), 0)
            self.assertTrue((root / "scripts" / "a.py").is_file())
            self.assertTrue((root / "tools" / "board_graph_tool.json").is_file())
            site_before = json.loads((root / "tools" / "board_graph_site.json").read_text(encoding="utf-8"))
            self.assertNotIn("tool_checkout", site_before["kicad_machine"])

            # Local edit refuses unvendor cleanly without deleting anything.
            (root / "scripts" / "a.py").write_bytes(b"print('edited')\n")
            with self.assertRaisesRegex(board_graph_tool.ToolError, "differs from"):
                board_graph_tool.unvendor(root)
            self.assertTrue((root / "tools" / "board_graph_tool.json").is_file())

            # Restore and run for real.
            (root / "scripts" / "a.py").write_bytes(b"print('a')\n")
            self.assertEqual(board_graph_tool.unvendor(root, mac_tool_checkout="~/board-graph"), 0)
            self.assertFalse((root / "scripts" / "a.py").exists())
            self.assertFalse((root / "tools" / "kicad" / "k.py").exists())
            self.assertFalse((root / "tools" / "kicad").exists())  # pruned empty dir
            self.assertFalse((root / "tools" / "board_graph_view" / "index.html").exists())
            self.assertFalse((root / "tools" / "board_graph_tool.json").exists())
            self.assertTrue((root / "tools" / "board_graph_view" / ".gitignore").is_file())
            self.assertTrue((root / "scripts" / "board_graph_project_actions.py").is_file())
            self.assertTrue((root / "tools" / "board_graph.json").is_file())
            self.assertTrue((root / "AGENTS.md").is_file())
            site_after = json.loads((root / "tools" / "board_graph_site.json").read_text(encoding="utf-8"))
            self.assertEqual(site_after["kicad_machine"]["tool_checkout"], "~/board-graph")

    def test_unvendor_refuses_mac_tool_checkout_on_one_machine(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self._make_vendored_project(root, one_machine=True)
            with self.assertRaisesRegex(board_graph_tool.ToolError, "one_machine is true"):
                board_graph_tool.unvendor(root, mac_tool_checkout="~/board-graph")


class CliExternalProjectTests(unittest.TestCase):
    def _build_external_project(self, root: pathlib.Path):
        (root / "board" / "reports").mkdir(parents=True)
        (root / "board" / "routing" / "tasks").mkdir(parents=True)
        (root / "board" / "footprints.pretty").mkdir(parents=True)
        (root / "board" / "datasheets").mkdir(parents=True)
        (root / "board" / "models").mkdir(parents=True)
        (root / "board" / "production").mkdir(parents=True)
        (root / "tools" / "board_graph_records").mkdir(parents=True)
        (root / "tools" / "board_graph_view").mkdir(parents=True)
        (root / "tools" / "board_graph_view" / ".gitignore").write_text("view.json\n", encoding="utf-8")

        (root / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")
        (root / "CLAUDE.md").write_text("# Claude\n", encoding="utf-8")
        (root / "board" / "DECISIONS.md").write_text("# Decisions\n", encoding="utf-8")
        (root / "board" / "bom.csv").write_text("Refs,Qty,Value,Manufacturer,MPN,Supplier,Supplier Part\n", encoding="utf-8")
        (root / "board" / "schematic_export.json").write_text("{}\n", encoding="utf-8")
        (root / "board" / "placement.md").write_text("# Placement\n", encoding="utf-8")
        (root / "board" / "drc-exclusions.json").write_text('{"accepted_warning_refs": []}\n', encoding="utf-8")
        (root / "board" / "parts.json").write_text("{}\n", encoding="utf-8")
        (root / "board" / "fp-lib-table").write_text("(fp_lib_table)\n", encoding="utf-8")
        (root / "board" / "demo.kicad_pro").write_text("{}\n", encoding="utf-8")
        (root / "board" / "demo.kicad_sch").write_text("(kicad_sch)\n", encoding="utf-8")
        (root / "board" / "demo.kicad_dru").write_text("(version 1)\n", encoding="utf-8")
        pcb = """(kicad_pcb
          (footprint "Conn" (layer "F.Cu") (at 0 0 0)
            (property "Reference" "J1" (at 0 0 0))
            (pad "1" smd roundrect (at 0 0 0) (size 0.5 1.5) (roundrect_rratio 0.25) (layers "F.Cu") (net 1 "/SIG"))
          )
          (footprint "Conn" (layer "F.Cu") (at 0 5 0)
            (property "Reference" "J2" (at 0 0 0))
            (pad "1" smd rect (at 0 0 0) (size 0.5 1.5) (layers "F.Cu") (net 1 "/SIG"))
          )
        )"""
        (root / "board" / "demo.kicad_pcb").write_text(pcb, encoding="utf-8")
        (root / "board" / "routing" / "tasks" / "01-sig.txt").write_text(
            "Connection: J1.1 J2.1 /SIG\nPad exits: centered\n", encoding="utf-8"
        )
        board_desc = {
            "name": "Demo",
            "project": "Demo",
            "package_name": "demo",
            "folder": "board",
            "schematic": "board/demo.kicad_sch",
            "pcb": "board/demo.kicad_pcb",
            "kicad_project": "board/demo.kicad_pro",
            "rules": "board/demo.kicad_dru",
            "footprint_table": "board/fp-lib-table",
            "footprint_library": "board/footprints.pretty",
            "bom_export": "board/bom.csv",
            "schematic_export": "board/schematic_export.json",
            "owner_placement": "board/placement.md",
            "reports": "board/reports",
            "production": "board/production",
            "drc_exclusions": "board/drc-exclusions.json",
            "parts_data": "board/parts.json",
            "datasheets": "board/datasheets",
            "decisions": "board/DECISIONS.md",
            "model_folders": ["board/models"],
            "hand_solder_notes": {
                "manual": "manual",
                "factory_parts": "factory",
                "when_unpopulated": "unpopulated",
                "when_factory": "factory",
            },
        }
        (root / "board" / "board.json").write_text(json.dumps(board_desc, indent=2), encoding="utf-8")
        (root / "tools" / "board_graph_site.json").write_text(
            json.dumps({"one_machine": True, "state_database": "tools/state.sqlite3"}), encoding="utf-8"
        )
        # Include docs/BOARD_EVIDENCE.md (absent from project!) so shared-doc fallback is exercised end-to-end.
        graph = {
            "version": 2,
            "milestone": "Demo",
            "stages": [{"id": "design", "label": "Design"}],
            "tracks": [{"id": "board", "label": "Board", "ordered": True, "start": "review"}],
            "stay_outcomes": {
                "more_work": "stay",
                "needs_owner": "stay",
                "needs_lead": "stay",
                "needs_skill": "stay",
            },
            "move_outcomes": {"pass": "move", "returned": "return"},
            "docs": [],
            "open_issues": [],
            "nodes": [
                {
                    "id": "review",
                    "label": "Review",
                    "stage": "design",
                    "track": "board",
                    "who": "owner",
                    "docs": ["docs/BOARD_EVIDENCE.md"],
                    "task": "Review demo board",
                    "pass_when": "looks good",
                    "owns": ["board/demo.kicad_pcb"],
                    "action": None,
                    "outcomes": {"pass": "done", "returned": []},
                }
            ],
        }
        (root / "tools" / "board_graph.json").write_text(json.dumps(graph, indent=2), encoding="utf-8")

    def _run_cli(self, cwd: pathlib.Path, *args: str) -> subprocess.CompletedProcess[str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("BOARD_GRAPH_")}
        return subprocess.run(
            [sys.executable, str(HERE / "board_graph_cli.py"), *args],
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_cli_drives_unvendored_project_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            origin = tmp_path / "origin.git"
            proj = tmp_path / "unvendored"
            self._build_external_project(proj)
            subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(proj), "init", "-b", "main"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(proj), "remote", "add", "origin", str(origin)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(proj), "add", "-A"], check=True, capture_output=True)
            subprocess.run(
                ["git", "-C", str(proj), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "init"],
                check=True,
                capture_output=True,
            )
            subprocess.run(["git", "-C", str(proj), "push", "-u", "origin", "main"], check=True, capture_output=True)
            state_path = tmp_path / "scratch.sqlite3"

            self.assertFalse((proj / "scripts").exists())
            self.assertFalse((proj / "tools" / "board_graph_tool.json").exists())
            self.assertFalse((proj / "docs" / "BOARD_EVIDENCE.md").exists())

            checked = self._run_cli(proj, "check")
            self.assertEqual(checked.returncode, 0, checked.stderr)

            where = self._run_cli(proj, "--state", str(state_path), "where")
            self.assertEqual(where.returncode, 0, where.stderr)
            self.assertIn("Review", where.stdout)

            started = self._run_cli(proj, "--state", str(state_path), "next", "--by", "owner")
            self.assertEqual(started.returncode, 2, started.stderr + started.stdout)
            self.assertIn("record review", started.stdout)
            self.assertIn("--project", started.stdout)
            self.assertIn("BOARD_EVIDENCE.md (", started.stdout)

            preflight = self._run_cli(proj, "route", "board/routing/tasks", "--preflight")
            self.assertEqual(preflight.returncode, 0, preflight.stderr)
            self.assertIn("preflight ok: 1 task(s)", preflight.stdout)

            snap = self._run_cli(proj, "view", "--state", str(state_path), "snapshot", "--quick")
            self.assertEqual(snap.returncode, 0, snap.stderr)
            self.assertTrue((proj / "tools" / "board_graph_view" / "view.json").is_file())

    def test_board_order_flags_and_kicad_subcommands(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            proj = tmp_path / "multiboard"
            self._build_external_project(proj)
            (proj / "tools" / "board_graph_boards.json").write_text(
                json.dumps({"first": "demo", "boards": {"demo": "board/board.json"}}),
                encoding="utf-8",
            )
            state_path = tmp_path / "scratch.sqlite3"

            for args in (
                ("--board", "demo", "where"),
                ("where", "--board", "demo"),
                ("--board", "demo", "check"),
                ("check", "--board", "demo"),
            ):
                res = self._run_cli(proj, *args)
                self.assertEqual(res.returncode, 0, f"{args}: {res.stderr + res.stdout}")

            for args in (
                ("--board", "demo", "route", "board/routing/tasks", "--preflight"),
                ("route", "--board", "demo", "board/routing/tasks", "--preflight"),
            ):
                res = self._run_cli(proj, *args)
                self.assertEqual(res.returncode, 0, f"{args}: {res.stderr + res.stdout}")
                self.assertIn("preflight ok: 1 task(s)", res.stdout)

            for args in (
                ("--board", "demo", "view", "--state", str(state_path), "snapshot", "--quick"),
                ("view", "--state", str(state_path), "snapshot", "--board", "demo", "--quick"),
            ):
                res = self._run_cli(proj, *args)
                self.assertEqual(res.returncode, 0, f"{args}: {res.stderr + res.stdout}")

            site_res = self._run_cli(proj, "site-settings")
            self.assertEqual(site_res.returncode, 0, site_res.stderr)
            self.assertIn("one_machine", site_res.stdout)

            desc_res = self._run_cli(proj, "--board", "demo", "board-description")
            self.assertEqual(desc_res.returncode, 0, desc_res.stderr)
            self.assertIn("Demo", desc_res.stdout)

            val_res = self._run_cli(proj, "validate", "--board", "demo", "--help")
            self.assertEqual(val_res.returncode, 0, val_res.stderr)

            render_help = self._run_cli(proj, "render", "--help")
            self.assertEqual(render_help.returncode, 0, render_help.stderr)

            render_leading = self._run_cli(proj, "--board", "demo", "render", "--help")
            self.assertEqual(render_leading.returncode, 0, render_leading.stderr)

            render_conflict = self._run_cli(proj, "--board", "demo", "render", "--board", "board/demo.kicad_pcb")
            self.assertEqual(render_conflict.returncode, 2)

            tc_help = self._run_cli(proj, "track-craft", "--help")
            self.assertEqual(tc_help.returncode, 0, tc_help.stderr)
            self.assertIn("track-craft", (tc_help.stdout + tc_help.stderr).lower())

    def test_kicad_run_rejects_comma_mangled_script_args_and_remote_windows_offline(self):
        pwsh = shutil.which("pwsh")
        if not pwsh or sys.platform != "win32":
            self.skipTest("Windows pwsh only")
        with tempfile.TemporaryDirectory() as tmp:
            proj = pathlib.Path(tmp) / "proj"
            self._build_external_project(proj)
            (proj / "board" / "demo.kicad_pro").write_text("{}\n", encoding="utf-8")
            (proj / "board" / "demo.kicad_pcb").write_text("(kicad_pcb (version 20241229))\n", encoding="utf-8")
            (proj / "board" / "demo.kicad_sch").write_text("(kicad_sch (version 20231120))\n", encoding="utf-8")

            mangled = self._run_cli(
                proj, "kicad-run", "-Mode", "Offline", "-Project", "board",
                "-Script", "tools/kicad/check_track_craft.py",
                "-ScriptArgs", '"board/demo.kicad_pcb","--json=out.json"',
            )
            self.assertNotEqual(mangled.returncode, 0)
            self.assertIn("comma-mangled", mangled.stderr + mangled.stdout)

            (proj / "tools" / "board_graph_site.json").write_text(
                json.dumps(test_site_settings.OTHER),
                encoding="utf-8",
            )
            refused = self._run_cli(
                proj, "kicad-run", "-Mode", "Offline", "-Project", "board",
                "-Script", "tools/kicad/check_track_craft.py",
                "--", "board/demo.kicad_pcb", "--output", "out.json",
            )
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("Refusing local Windows Offline KiCad Python", refused.stderr + refused.stdout)


class ExternalRoutingAndServeTests(unittest.TestCase):
    def test_external_routing_wrapper_and_prepare_mac(self):
        import argparse
        fake_site = site_settings.Settings(
            site_settings.validate(dict(test_site_settings.OTHER, kicad_machine=dict(
                test_site_settings.OTHER["kicad_machine"], tool_checkout="~/board-graph"
            ))),
            "site.json",
        )
        args = argparse.Namespace(
            mac_steps="~/other-steps", minutes=3, provider="codex", review_provider="gemini",
            model="gpt-6.1-terra", effort="low", review_model="gemini-4-neon-high", agent=None,
        )
        with (mock.patch.object(routing_run.tool_paths, "is_external", return_value=True),
              mock.patch.object(routing_run.mac, "SITE", fake_site),
              mock.patch.object(routing_run.mac, "CHECKOUT", fake_site.checkout),
              mock.patch.object(routing_run.mac, "TOOL_CHECKOUT", "~/board-graph")):
            script = routing_run.wrapper(args, "abc123", "~/other-route", "board/tasks", "deadbeef", tool_commit="cafe1234")
            self.assertIn('export BOARD_GRAPH_PROJECT="$PWD"', script)
            self.assertIn('tool_tree="$HOME/board-graph"', script)
            self.assertIn('git -C "$tool_tree" rev-parse HEAD', script)
            self.assertIn('default_ipc_py="$HOME/board-graph/.venv-kicad-ipc/bin/python"', script)
            self.assertIn('ipc_python="${KICAD_IPC_PYTHON:-$default_ipc_py}"', script)
            self.assertIn('"$ipc_python" "$HOME/board-graph/scripts/editor_step.py"', script)

            # prepare_mac syncs Mac tool checkout to exact desktop commit and builds venv in tool_tree.
            with (mock.patch.object(routing_run.board_graph_tool, "load", return_value={"version": "0.20.0", "files": {}}),
                  mock.patch.object(routing_run.board_graph_tool, "differences", return_value=[]),
                  mock.patch.object(routing_run.tool_paths, "tool_dirty", return_value=[]),
                  mock.patch.object(routing_run.tool_paths, "tool_commit", return_value="1" * 40),
                  mock.patch.object(routing_run, "git_in", return_value="2" * 40),
                  mock.patch.object(routing_run, "mac_bash") as sent):
                meta = routing_run.prepare_mac("feat/route", "3" * 40, "~/other-route")
            self.assertEqual(meta["tool_commit"], "1" * 40)
            self.assertTrue(meta["external_tool"])
            mac_script = sent.call_args.args[0]
            self.assertIn('tool_tree="$HOME/board-graph"', mac_script)
            self.assertIn('git -C "$tool_tree" checkout -q --detach ' + ("1" * 40), mac_script)
            self.assertIn('$tool_tree/.venv-kicad-ipc/bin/python', mac_script)

            # Dirty desktop tool checkout refuses prepare_mac before touching Mac.
            with (mock.patch.object(routing_run.board_graph_tool, "load", return_value={"version": "0.20.0", "files": {}}),
                  mock.patch.object(routing_run.board_graph_tool, "differences", return_value=[]),
                  mock.patch.object(routing_run.tool_paths, "tool_dirty", return_value=[" M scripts/pad_exits.py"])):
                with self.assertRaisesRegex(routing_run.Refused, "uncommitted changes"):
                    routing_run.prepare_mac("feat/route", "3" * 40, "~/other-route")

    def test_serve_plist_and_fallback_index_in_external_mode(self):
        data = copy.deepcopy(test_site_settings.OTHER)
        data["kicad_machine"]["tool_checkout"] = "~/board-graph"
        settings = site_settings.Settings(site_settings.validate(data), "site.json")
        plist = plistlib.loads(board_graph_serve.written_plist("/Users/kicad/other-project", settings))
        self.assertEqual(plist["ProgramArguments"], ["/usr/bin/python3", "/Users/kicad/board-graph/scripts/board_graph_serve.py"])
        self.assertEqual(plist["WorkingDirectory"], "/Users/kicad/other-project")
        self.assertEqual(plist["EnvironmentVariables"], {"BOARD_GRAPH_PROJECT": "/Users/kicad/other-project"})

        with tempfile.TemporaryDirectory() as tmp:
            proj = pathlib.Path(tmp)
            view_dir = proj / "tools" / "board_graph_view"
            view_dir.mkdir(parents=True)
            (view_dir / "view.json").write_text('{"ok": true}\n', encoding="utf-8")
            self.assertEqual(board_graph_serve.stamps(view_dir, external=False)["page"], 0)
            self.assertGreater(board_graph_serve.stamps(view_dir, external=True)["page"], 0)

            with mock.patch.dict(os.environ, {tool_paths.ENV: str(proj)}):
                srv = board_graph_serve.server("127.0.0.1", 0, view_dir, repo_root=proj)
                thread = threading.Thread(target=srv.serve_forever, daemon=True)
                thread.start()
                try:
                    port = srv.server_address[1]
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    conn.request("GET", "/")
                    resp = conn.getresponse()
                    body = resp.read()
                    self.assertEqual(resp.status, 200)
                    self.assertIsNotNone(resp.getheader("Last-Modified"))
                    self.assertIn(b"BOARD_GRAPH_SNAPSHOT", body)
                    conn.close()
                finally:
                    srv.shutdown()
                    srv.server_close()

    def test_operator_env_and_transcript_match_external_script_paths(self):
        self.assertIn(tool_paths.ENV, routing_codex.OPERATOR_ENV)
        self.assertIn(tool_paths.ENV, routing_gemini.OPERATOR_ENV)
        self.assertIn("--adopt-from", board_graph_cli.VALUE_FLAGS)
        self.assertIn("--mac-tree", board_graph_cli.VALUE_FLAGS)
        selected, kept = board_graph_cli._extract_flag(
            ["route", "board/tasks", "--adopt-from", "e4168357", "--board", "strip", "--mac-tree", "~/t"],
            "--board",
        )
        self.assertEqual((selected, kept), ("strip", ["route", "board/tasks", "--adopt-from", "e4168357", "--mac-tree", "~/t"]))
        ext = "/Users/kicad/board-graph/scripts"
        lines = [
            json.dumps({
                "message": {
                    "content": [
                        {"type": "tool_use", "id": "1", "name": "Bash",
                         "input": {"command": f".venv-kicad-ipc/bin/python {ext}/kicad_route.py R1.1 R2.1"}},
                        {"type": "tool_result", "tool_use_id": "1", "content": '{"status":"closed"}'},
                        {"type": "tool_use", "id": "2", "name": "Bash",
                         "input": {"command": f".venv-kicad-ipc/bin/python {ext}/mac.py look"}},
                        {"type": "tool_result", "tool_use_id": "2", "content": "/tmp/look.png"},
                    ]
                }
            })
        ]
        found = editor_step.calls(lines)
        self.assertEqual((found["route"], found["route_closed"], found["look"], found["other"]), (1, 1, 1, 0))

        codex_found = routing_codex.audit_commands([
            (f"/Users/kicad/board-graph/.venv-kicad-ipc/bin/python {ext}/kicad_route.py R1.1 R2.1",
             '{"status":"closed"}', 0, "completed"),
            (f"/Users/kicad/board-graph/.venv-kicad-ipc/bin/python {ext}/mac.py look \"PCB Editor\"",
             "/tmp/look.png", 0, "completed"),
        ], board_stem="demo")
        self.assertTrue(codex_found["looked_after_edit"])
        self.assertEqual(codex_found["wrapper_calls"], 0)

    def test_approvals_resolves_shared_tool_doc_in_external_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = pathlib.Path(tmp).resolve()
            graph_data = {
                "version": 2,
                "stages": [{"id": "design", "label": "Design"}],
                "tracks": [{"id": "board", "label": "Board", "ordered": True, "start": "r"}],
                "stay_outcomes": {
                    "more_work": "stay",
                    "needs_owner": "stay",
                    "needs_lead": "stay",
                    "needs_skill": "stay",
                },
                "move_outcomes": {"pass": "move", "returned": "return"},
                "open_issues": [],
                "nodes": [{"id": "r", "label": "R", "stage": "design", "track": "board", "who": "owner",
                           "docs": ["docs/BOARD_EVIDENCE.md"], "task": "t", "pass_when": "p", "owns": [],
                           "action": None, "outcomes": {"pass": "done", "returned": []}}],
            }
            graph_file = proj / "graph.json"
            graph_file.write_text(json.dumps(graph_data), encoding="utf-8")
            import board_graph
            graph = board_graph.Graph(graph_file)
            sha = lambda p: hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()
            with mock.patch.dict(os.environ, {tool_paths.ENV: str(proj)}):
                approvals = board_approvals.Approvals(graph, [], proj, {}, sha)
                self.assertEqual(
                    approvals.source("file", "docs/BOARD_EVIDENCE.md"),
                    sha(TOOL_ROOT / "docs" / "BOARD_EVIDENCE.md"),
                )
                self.assertIsNone(approvals.source("owned", "docs/BOARD_EVIDENCE.md"))
                self.assertIsNone(approvals.source("file", "docs/PRODUCT_TRUTH.md"))
                self.assertIsNone(approvals.source("file", "../docs/BOARD_EVIDENCE.md"))

    def test_render_board_views_resolves_board_name_and_uses_external_tool_on_mac(self):
        import io
        import sys
        import tarfile
        sys.path.insert(0, str(TOOL_ROOT / "tools" / "kicad"))
        import render_board_views
        with tempfile.TemporaryDirectory() as tmp:
            proj = pathlib.Path(tmp) / "proj"
            CliExternalProjectTests()._build_external_project(proj)
            (proj / "tools" / "board_graph_boards.json").write_text(
                json.dumps({"first": "demo", "boards": {"demo": "board/board.json"}}),
                encoding="utf-8",
            )
            expected_pcb = (proj / "board" / "demo.kicad_pcb").resolve()
            clean_env = {k: v for k, v in os.environ.items() if not k.startswith("BOARD_GRAPH_")}
            with mock.patch.dict(os.environ, clean_env, clear=True):
                self.assertEqual(render_board_views.resolve_board_arg("demo", root=proj), expected_pcb)
                self.assertEqual(render_board_views.resolve_board_arg("board", root=proj), expected_pcb)
                self.assertEqual(render_board_views.resolve_board_arg(str(expected_pcb), root=proj), expected_pcb)

            tar_buf = io.BytesIO()
            with tarfile.open(fileobj=tar_buf, mode="w") as archive:
                png_bytes = b"\x89PNG\r\n\x1a\n"
                info = tarfile.TarInfo("front_copper.png")
                info.size = len(png_bytes)
                archive.addfile(info, io.BytesIO(png_bytes))
            ssh_calls = []

            def fake_ssh(command, stdin_text=None, **kwargs):
                ssh_calls.append((command, stdin_text))
                stdout = tar_buf.getvalue() if command == "bash -s" else b""
                return subprocess.CompletedProcess(["ssh"], 0, stdout=stdout, stderr=b"")

            import mac as mac_mod
            out_dir = pathlib.Path(tmp) / "out"
            out_dir.mkdir()
            with mock.patch.object(render_board_views, "ROOT", proj), \
                    mock.patch.object(mac_mod, "CHECKOUT", "~/demo-board"), \
                    mock.patch.object(mac_mod, "TOOL_CHECKOUT", "~/board-graph"), \
                    mock.patch.object(mac_mod, "ssh", side_effect=fake_ssh):
                render_board_views.render_on_mac(expected_pcb, out_dir)
            self.assertTrue((out_dir / "front_copper.png").is_file())
            bash_script = [stdin for cmd, stdin in ssh_calls if cmd == "bash -s"][0]
            self.assertIn('BOARD_GRAPH_PROJECT="$PWD" python3 ~/board-graph/tools/kicad/render_board_views.py', bash_script)


class PackagedWheelAndInitTests(unittest.TestCase):
    def test_cli_version_prints_manifest_version(self):
        proc = subprocess.run(
            [sys.executable, str(HERE / "board_graph_cli.py"), "version"],
            cwd=str(tempfile.gettempdir()),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        expected_version = board_graph_tool.load(TOOL_ROOT)["version"]
        self.assertIn(f"board-graph {expected_version}", proc.stdout)

    def test_cli_init_scaffolds_empty_folder_without_clone(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = pathlib.Path(tmp).resolve()
            proc = subprocess.run(
                [sys.executable, str(HERE / "board_graph_cli.py"), "init"],
                cwd=str(proj),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue((proj / "AGENTS.md").is_file())
            self.assertTrue((proj / "tools" / "board_graph.json").is_file())
            self.assertTrue((proj / "board" / "board.json").is_file())
            self.assertTrue((proj / ".agents" / "skills" / "kicad-10-operator" / "SKILL.md").is_file())
            self.assertTrue((proj / ".gitattributes").is_file())
            self.assertTrue((proj / ".gitignore").is_file())
            (proj / "AGENTS.md").write_text("# Custom\n", encoding="utf-8")
            again = subprocess.run(
                [sys.executable, str(HERE / "board_graph_cli.py"), "init"],
                cwd=str(proj),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertEqual((proj / "AGENTS.md").read_text(encoding="utf-8"), "# Custom\n")

    def test_tool_paths_non_git_wheel_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.assertFalse(tool_paths.is_git_checkout(root))
            self.assertIsNone(tool_paths.tool_commit(root))
            self.assertEqual(tool_paths.tool_dirty(root), [])

    def test_routing_run_prepare_supports_packaged_wheel_desktop(self):
        fake_site = site_settings.Settings(
            site_settings.validate(dict(test_site_settings.OTHER, kicad_machine=dict(
                test_site_settings.OTHER["kicad_machine"], tool_checkout="~/board-graph"
            ))),
            "site.json",
        )
        manifest_bytes = json.dumps({"name": "board-graph", "version": "0.32.0", "files": {}}).encode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            fake_root = pathlib.Path(tmp)
            (fake_root / "tools").mkdir()
            (fake_root / "tools" / "board_graph_tool.json").write_bytes(manifest_bytes)
            with (mock.patch.object(routing_run.tool_paths, "is_external", return_value=True),
                  mock.patch.object(routing_run.tool_paths, "is_git_checkout", return_value=False),
                  mock.patch.object(routing_run, "TOOL_ROOT", fake_root),
                  mock.patch.object(routing_run.mac, "SITE", fake_site),
                  mock.patch.object(routing_run.mac, "CHECKOUT", fake_site.checkout),
                  mock.patch.object(routing_run.mac, "TOOL_CHECKOUT", "~/board-graph"),
                  mock.patch.object(routing_run.board_graph_tool, "load", return_value={"version": "0.32.0", "files": {}}),
                  mock.patch.object(routing_run.board_graph_tool, "differences", return_value=[]),
                  mock.patch.object(routing_run, "mac_bash") as sent):
                meta = routing_run.prepare_mac("feat/route", "3" * 40, "~/other-route")
        self.assertTrue(meta["external_tool"])
        self.assertIsNone(meta["tool_commit"])
        self.assertEqual(meta["manifest_sha"], board_graph_tool.digest(manifest_bytes))
        mac_script = sent.call_args.args[0]
        self.assertIn("git -C \"$tool_tree\" fetch -q --tags origin", mac_script)
        self.assertIn("v0.32.0", mac_script)

    def test_board_graph_entry_resolves_editable_and_packaged_data(self):
        if str(TOOL_ROOT) not in sys.path:
            sys.path.insert(0, str(TOOL_ROOT))
        import board_graph_entry
        self.assertEqual(board_graph_entry._resolve_scripts_dir(TOOL_ROOT).resolve(), HERE.resolve())
        with tempfile.TemporaryDirectory() as tmp:
            wheel_base = pathlib.Path(tmp) / "site-packages"
            pkg_scripts = wheel_base / "_board_graph_data" / "scripts"
            pkg_scripts.mkdir(parents=True)
            (pkg_scripts / "board_graph_cli.py").write_text("# cli\n", encoding="utf-8")
            self.assertEqual(board_graph_entry._resolve_scripts_dir(wheel_base).resolve(), pkg_scripts.resolve())

    def test_tool_dirty_ignores_ephemeral_kicad_prl_sidecars(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=str(root), check=True)
            (root / "tracked.txt").write_text("ok\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=str(root), check=True)
            subprocess.run(
                ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-q", "-m", "init"],
                cwd=str(root),
                check=True,
            )
            (root / "board").mkdir()
            (root / "board" / "example.kicad_prl").write_text("{}\n", encoding="utf-8")
            self.assertEqual(tool_paths.tool_dirty(root), [])
            (root / "board" / "real_change.txt").write_text("dirty\n", encoding="utf-8")
            self.assertEqual(tool_paths.tool_dirty(root), ["?? board/"])

    def test_external_tool_sync_checks_and_wrapper_share_dirty_excludes(self):
        dirty_args = tool_paths.dirty_status_shell_args()
        commit_script = routing_run._external_tool_sync_checks({"version": "0.32.1"}, "a" * 40, "b" * 40, None)
        tag_script = routing_run._external_tool_sync_checks({"version": "0.32.1"}, None, "", "c" * 64)
        for script in (commit_script, tag_script):
            self.assertIn(f"status --porcelain {dirty_args}", script)
        args = mock.Mock(minutes=6, provider="gemini", review_provider="gemini", model=None,
                         effort=None, review_model=None, agent=None, batch_minutes=None)
        with (mock.patch.object(routing_run.tool_paths, "is_external", return_value=True),
              mock.patch.object(routing_run.mac, "TOOL_CHECKOUT", "~/board-graph")):
            wrap = routing_run.wrapper(args, "att1", "~/tree", "tasks/b1", "1" * 40, tool_commit="a" * 40)
        self.assertIn(f"status --porcelain {dirty_args}", wrap)

    def test_board_space_accepts_external_project_argument(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = pathlib.Path(tmp).resolve()
            (proj / "board").mkdir()
            pcb = proj / "board" / "custom.kicad_pcb"
            pcb.write_text("(kicad_pcb (version 20241229))\n", encoding="utf-8")
            board_desc = {
                "name": "custom",
                "project": "Custom",
                "package_name": "CUSTOM",
                "folder": "board",
                "schematic": "board/custom.kicad_sch",
                "pcb": "board/custom.kicad_pcb",
                "kicad_project": "board/custom.kicad_pro",
                "rules": "board/custom.kicad_dru",
                "footprint_table": "board/fp-lib-table",
                "footprint_library": "board/custom.pretty",
                "model_folders": ["board/models"],
                "bom_export": "board/exports/custom-bom.csv",
                "schematic_export": "board/exports/custom-schematic.pdf",
                "owner_placement": "board/owner_placement.json",
                "reports": "board/reports",
                "production": "board/production",
                "drc_exclusions": "board/reports/documented-drc-exclusions.json",
                "parts_data": "hardware/reference/parts.json",
                "datasheets": "hardware/reference",
                "decisions": "board/DECISIONS.md",
                "hand_solder_notes": {"manual": {}, "factory_parts": [], "when_unpopulated": [], "when_factory": []},
            }
            (proj / "board" / "board.json").write_text(json.dumps(board_desc), encoding="utf-8")
            env = {k: v for k, v in os.environ.items() if not k.startswith("BOARD_GRAPH_")}
            proc = subprocess.run(
                [sys.executable, str(HERE / "board_space.py"), "--project", str(proj), "--json"],
                cwd=str(tempfile.gettempdir()),
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout), [])


if __name__ == "__main__":
    unittest.main()
