#!/usr/bin/env python3
"""Unit tests for Linux + Xvfb Mode: ipc runner support across kicad_install, editor_step_kicad, kicad_reload_board, mac, routing providers, and routing_run."""
from __future__ import annotations

import argparse
import importlib
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
ROOT = SCRIPTS_DIR.parent
KICAD_TOOLS_DIR = ROOT / "tools" / "kicad"
for _p in (SCRIPTS_DIR, KICAD_TOOLS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

sys.modules.setdefault("kipy", SimpleNamespace(KiCad=object))

import editor_step_kicad  # noqa: E402
import kicad_install  # noqa: E402
import kicad_reload_board  # noqa: E402
import mac  # noqa: E402
import routing_codex  # noqa: E402
import routing_gemini  # noqa: E402
import routing_run  # noqa: E402


class KicadInstallLinuxTests(unittest.TestCase):
    def test_linux_paths_and_env_overrides(self):
        with mock.patch.object(sys, "platform", "linux"):
            with mock.patch.dict(os.environ, {}, clear=False):
                for key in ("KICAD_CLI", "KICAD_PCBNEW", "KICAD_PYTHON"):
                    os.environ.pop(key, None)
                with mock.patch("shutil.which", return_value=None):
                    reloaded = importlib.reload(kicad_install)
                    self.assertEqual(reloaded.KICAD_CLI.as_posix(), "/usr/bin/kicad-cli")
                    self.assertEqual(reloaded.KICAD_PCBNEW.as_posix(), "/usr/bin/pcbnew")
                    self.assertEqual(reloaded.KICAD_PYTHON.as_posix(), "/usr/bin/python3")
                    self.assertEqual(reloaded.KICAD_SHARE.as_posix(), "/usr/share/kicad")
            with mock.patch.dict(
                os.environ,
                {
                    "KICAD_CLI": "/opt/kicad/bin/kicad-cli",
                    "KICAD_PCBNEW": "/opt/kicad/bin/pcbnew",
                    "KICAD_PYTHON": "/opt/kicad/bin/python3",
                },
                clear=False,
            ):
                reloaded = importlib.reload(kicad_install)
                self.assertEqual(reloaded.KICAD_CLI.as_posix(), "/opt/kicad/bin/kicad-cli")
                self.assertEqual(reloaded.KICAD_PCBNEW.as_posix(), "/opt/kicad/bin/pcbnew")
                self.assertEqual(reloaded.KICAD_PYTHON.as_posix(), "/opt/kicad/bin/python3")
        importlib.reload(kicad_install)


class EditorStepKicadLinuxTests(unittest.TestCase):
    def test_screen_locked_returns_false_on_linux(self):
        with mock.patch.object(editor_step_kicad.sys, "platform", "linux"):
            self.assertFalse(editor_step_kicad.screen_locked())

    def test_lock_and_is_locked_toggle_write_bits_on_linux(self):
        with tempfile.TemporaryDirectory() as td:
            board = pathlib.Path(td) / "demo.kicad_pcb"
            board.write_text("(kicad_pcb)\n", encoding="utf-8")
            os.chmod(board, stat.S_IRUSR | stat.S_IWUSR)
            with mock.patch.object(editor_step_kicad.sys, "platform", "linux"):
                self.assertFalse(editor_step_kicad.is_locked(board))
                editor_step_kicad.lock(board, True)
                self.assertTrue(editor_step_kicad.is_locked(board))
                editor_step_kicad.lock(board, False)
                self.assertFalse(editor_step_kicad.is_locked(board))

    def test_picture_of_editor_linux_uses_import_and_checks_png(self):
        with tempfile.TemporaryDirectory() as td:
            out = pathlib.Path(td) / "editor.png"
            calls = []

            def fake_run(cmd, **_kwargs):
                calls.append(cmd)
                if cmd[0] == "import":
                    out.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

            with (
                mock.patch.object(editor_step_kicad.sys, "platform", "linux"),
                mock.patch.object(editor_step_kicad.time, "sleep"),
                mock.patch.object(editor_step_kicad.subprocess, "run", side_effect=fake_run),
                mock.patch.object(
                    mac,
                    "windows",
                    return_value=[{"number": 42, "app": "pcbnew", "pid": 999, "title": "demo - PCB Editor", "bounds": [0, 0, 800, 600]}],
                ),
            ):
                editor_step_kicad.picture_of_editor(999, out, "demo")
            self.assertEqual(calls[0], ["xdotool", "windowactivate", "--sync", "42"])
            self.assertEqual(calls[1], ["import", "-window", "42", str(out)])

            def fake_bad_run(cmd, **_kwargs):
                if cmd[0] == "import":
                    out.write_bytes(b"not-a-png")
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

            with (
                mock.patch.object(editor_step_kicad.sys, "platform", "linux"),
                mock.patch.object(editor_step_kicad.time, "sleep"),
                mock.patch.object(editor_step_kicad.subprocess, "run", side_effect=fake_bad_run),
                mock.patch.object(
                    mac,
                    "windows",
                    return_value=[{"number": 42, "app": "pcbnew", "pid": 999, "title": "demo — PCB Editor", "bounds": [0, 0, 800, 600]}],
                ),
                self.assertRaisesRegex(ValueError, "invalid editor capture"),
            ):
                editor_step_kicad.picture_of_editor(999, out, "demo")


class ReloadBoardLinuxTests(unittest.TestCase):
    def test_editor_titles_requires_display_and_parses_xdotool(self):
        with mock.patch.object(kicad_reload_board.sys, "platform", "linux"):
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("DISPLAY", None)
                with self.assertRaisesRegex(RuntimeError, "DISPLAY is not set"):
                    kicad_reload_board.editor_titles(123)

            def fake_xdotool(cmd, **_kwargs):
                if cmd == ["xdotool", "search", "--onlyvisible", "--pid", "123"]:
                    return subprocess.CompletedProcess(cmd, 0, stdout="101\n102\n", stderr="")
                if cmd == ["xdotool", "getwindowname", "101"]:
                    return subprocess.CompletedProcess(cmd, 0, stdout="demo - PCB Editor\n", stderr="")
                if cmd == ["xdotool", "getwindowname", "102"]:
                    return subprocess.CompletedProcess(cmd, 0, stdout="\n", stderr="")
                raise AssertionError(cmd)

            with (
                mock.patch.dict(os.environ, {"DISPLAY": ":99"}, clear=False),
                mock.patch.object(kicad_reload_board.shutil, "which", return_value="/usr/bin/xdotool"),
                mock.patch.object(kicad_reload_board.subprocess, "run", side_effect=fake_xdotool),
            ):
                self.assertEqual(kicad_reload_board.editor_titles(123), ["demo - PCB Editor", ""])

            with (
                mock.patch.dict(os.environ, {"DISPLAY": ":99"}, clear=False),
                mock.patch.object(kicad_reload_board.shutil, "which", return_value="/usr/bin/xdotool"),
                mock.patch.object(
                    kicad_reload_board.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 1, stdout="", stderr=""),
                ),
            ):
                self.assertEqual(kicad_reload_board.editor_titles(123), [])


class MacAndRoutingLinuxTests(unittest.TestCase):
    def test_pcb_window_title_accepts_emdash_and_hyphen_and_rejects_unsaved(self):
        self.assertTrue(mac.pcb_window_title("demo — PCB Editor", "demo"))
        self.assertTrue(mac.pcb_window_title("demo [Read Only] — PCB Editor", "demo"))
        self.assertTrue(mac.pcb_window_title("demo - PCB Editor", "demo"))
        self.assertTrue(mac.pcb_window_title("demo [Read Only] - PCB Editor", "demo"))
        self.assertTrue(mac.pcb_window_title("*demo - PCB Editor", "demo"))
        self.assertFalse(mac.pcb_window_title("other - PCB Editor", "demo"))

    def test_operator_env_forwards_display_and_xauthority(self):
        for mod in (routing_gemini, routing_codex):
            self.assertIn("DISPLAY", mod.OPERATOR_ENV)
            self.assertIn("XAUTHORITY", mod.OPERATOR_ENV)

    def test_wrapper_and_prepare_mac_support_linux_one_machine_and_kicad_ipc_python(self):
        args = argparse.Namespace(
            mac_steps="~/steps",
            minutes=3,
            model=None,
            effort="low",
            provider=None,
            review_model=None,
            agent='eval "$TASK"',
        )
        with (
            mock.patch.object(routing_run.tool_paths, "is_external", return_value=True),
            mock.patch.object(routing_run.mac, "TOOL_CHECKOUT", None),
            mock.patch.object(routing_run.mac, "ONE_MACHINE", True),
            mock.patch.object(routing_run, "TOOL_ROOT", pathlib.PurePosixPath("/opt/board-graph")),
        ):
            text = routing_run.wrapper(args, "abc", "/repo", "board/tasks", "c0ffee", tool_commit=None)
            self.assertIn("tool_tree=/opt/board-graph", text)
            self.assertIn("default_ipc_py=/opt/board-graph/.venv-kicad-ipc/bin/python", text)
            self.assertIn('ipc_python="${KICAD_IPC_PYTHON:-$default_ipc_py}"', text)
            self.assertIn('"$ipc_python" /opt/board-graph/scripts/editor_step.py', text)

        seen_scripts = []
        with (
            mock.patch.object(routing_run, "_external_tool_mode", return_value=False),
            mock.patch.object(routing_run.board_graph_tool, "load", return_value={"version": "0.30.0"}),
            mock.patch.object(routing_run.board_graph_tool, "differences", return_value=[]),
            mock.patch.object(routing_run, "git", return_value="deadbeef"),
            mock.patch.object(routing_run, "mac_bash", side_effect=lambda s, timeout=300: seen_scripts.append(s) or ""),
        ):
            routing_run.prepare_mac("feat/x", "abc123", "/tmp/tree")
        self.assertIn('if [ "$(uname -s)" = "Darwin" ]; then', seen_scripts[0])
        self.assertIn('ipc_py="${KICAD_IPC_PYTHON:-$main/.venv-kicad-ipc/bin/python}"', seen_scripts[0])

    def test_mac_screen_inline_python_compiles_and_formats_windows(self):
        import contextlib
        import io
        import re

        script = (SCRIPTS_DIR / "mac_screen.sh").read_text(encoding="utf-8")
        snippets = re.findall(r"python3 -c '([^']+)'", script)
        self.assertGreaterEqual(len(snippets), 3)
        for idx, snippet in enumerate(snippets):
            compile(snippet, f"mac_screen.sh#{idx}", "exec")

        buf = io.StringIO()
        sample = json.dumps([{"number": 42, "app": "PCB Editor", "title": "demo — PCB Editor"}])
        with mock.patch.object(sys, "stdin", io.StringIO(sample)), contextlib.redirect_stdout(buf):
            exec(compile(snippets[0], "mac_screen.sh#0", "exec"), {})
        self.assertEqual(buf.getvalue().strip(), "42\tPCB Editor\tdemo — PCB Editor")

    def test_audit_commands_accepts_linux_hyphen_pcb_window_titles(self):
        for title in (
            "demo - PCB Editor",
            "demo [Read Only] - PCB Editor",
            "*demo - PCB Editor",
            "demo — PCB Editor",
            "demo [Read Only] — PCB Editor",
        ):
            with self.subTest(title=title):
                out = f"/tmp/look.png\n1600 x 900 px of window #42 (PCB Editor: {title}); 1 px = 1.000 points.\n"
                entries = [
                    ("python3 scripts/kicad_lay.py route J1.1 J2.1 --layer F.Cu", '{"ok":true,"status":"laid"}', 0, "completed", 0.5),
                    ('python3 scripts/mac.py look "PCB Editor"', out, 0, "completed", 0.2),
                ]
                audited = routing_codex.audit_commands(entries, board_stem="demo")
                self.assertEqual(audited["look"], 1)
                self.assertTrue(audited["looked_after_edit"])
        bad_out = "/tmp/look.png\n1600 x 900 px of window #42 (PCB Editor: other - PCB Editor); 1 px = 1.000 points.\n"
        bad = routing_codex.audit_commands([('python3 scripts/mac.py look "PCB Editor"', bad_out, 0, "completed", 0.2)], board_stem="demo")
        self.assertEqual(bad["look"], 0)

    def test_default_claude_bin_and_poll_seconds_and_linux_vm_tools(self):
        import editor_step
        with mock.patch.dict(os.environ, {"BOARD_GRAPH_CLAUDE_BIN": "/opt/claude/bin/claude"}, clear=False):
            self.assertEqual(editor_step._default_claude_bin(), "/opt/claude/bin/claude")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BOARD_GRAPH_CLAUDE_BIN", None)
            with mock.patch.object(editor_step.shutil, "which", return_value="/usr/local/bin/claude"):
                self.assertEqual(editor_step._default_claude_bin(), "/usr/local/bin/claude")
            with mock.patch.object(editor_step.shutil, "which", return_value=None):
                self.assertTrue(editor_step._default_claude_bin().endswith("claude"))

        with mock.patch.object(routing_run.mac, "ONE_MACHINE", True):
            self.assertEqual(routing_run.poll_seconds(), routing_run.LOCAL_POLL)
        with mock.patch.object(routing_run.mac, "ONE_MACHINE", False):
            self.assertEqual(routing_run.poll_seconds(), routing_run.POLL)

        bootstrap = ROOT / "docker" / "bootstrap_linux_vm.sh"
        self.assertTrue(bootstrap.is_file())
        self.assertNotIn(b"\r\n", bootstrap.read_bytes())

        import contextlib
        import sqlite3
        sys.path.insert(0, str(ROOT / "docker"))
        import benchmark_report  # noqa: E402
        with tempfile.TemporaryDirectory() as td:
            repo = pathlib.Path(td)
            rec_dir = repo / "tools" / "board_graph_records"
            rec_dir.mkdir(parents=True)
            (rec_dir / "01.json").write_text(json.dumps({
                "at": "2026-11-27T10:00:00+00:00", "track": "board", "node": "inventory",
                "outcome": "pass", "target": "decisions", "actor": "script:baseline", "note": "ok",
                "evidence": [], "inputs": {},
            }), encoding="utf-8")
            (rec_dir / "02.json").write_text(json.dumps({
                "at": "2026-11-27T10:01:30+00:00", "track": "board", "node": "routing_batch",
                "outcome": "needs_lead", "target": "routing_batch", "actor": "agent:codex", "note": "blocked",
                "evidence": [], "inputs": {},
            }), encoding="utf-8")
            db_path = repo / "logs" / "board-graph" / "state.sqlite3"
            db_path.parent.mkdir(parents=True)
            with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
                conn.execute("CREATE TABLE upkeep(run_id TEXT, node TEXT, why TEXT, started_at TEXT, finished_at TEXT)")
                conn.execute("CREATE TABLE attempts(started_at TEXT, finished_at TEXT, tokens INTEGER)")
                conn.execute("INSERT INTO upkeep VALUES('u1','inventory','start','2026-11-27T10:00:00+00:00','2026-11-27T10:00:05+00:00')")
                conn.execute("INSERT INTO attempts VALUES('2026-11-27T10:00:10+00:00','2026-11-27T10:01:10+00:00', 1200)")
                conn.commit()
            rdir = repo / "board" / "routing"
            rdir.mkdir(parents=True)
            (rdir / "run-12345678.jsonl").write_text(json.dumps({
                "summary": True,
                "timing": {"total_s": 20.0, "llm_s": 12.0, "tool_s": 3.0, "review_s": 4.0, "harness_s": 1.0, "refusal_s": 0.5, "thinking_tokens": 500},
                "tokens_total": {"input": 100, "output": 50, "cache_read": 0, "cache_write": 0},
                "refusals": {"by_reason": {"kicad_lay: drc_failed": {"count": 2}}},
            }) + "\n", encoding="utf-8")
            summary = benchmark_report.summarize_run("codex", repo)
            self.assertEqual(summary["wall_s"], 90.0)
            self.assertEqual(summary["needs_lead"], 1)
            self.assertEqual(summary["sqlite"]["upkeep_s"], 5.0)
            self.assertEqual(summary["routing"]["timing"]["llm_s"], 12.0)
            md = benchmark_report.format_markdown([summary])
            self.assertIn("| codex |", md)


if __name__ == "__main__":
    unittest.main()
