#!/usr/bin/env python3
"""Tests for board-graph Hub multi-run server, SSE updates, and tool_paths.tool_status."""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import board_graph_cli  # noqa: E402
import board_graph_hub  # noqa: E402
import board_graph_serve  # noqa: E402
import tool_paths  # noqa: E402


class BoardGraphHubTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._td.name)

    def tearDown(self) -> None:
        self._td.cleanup()

    def _make_sample_view(self, name: str, pos: str = "schematic") -> dict:
        return {
            "project": name,
            "tool": {"version": "0.34.0", "branch": f"exp/{name}", "commit": "abc1234", "ahead": 1, "dirty": False,
                     "commits_ahead": [{"sha": "abc1234", "subject": "Fix preflight"}]},
            "graph": {
                "nodes": [
                    {"id": "inventory", "track": "board", "label": "Inventory", "who": "auto", "task": "Read files"},
                    {"id": "schematic", "track": "board", "label": "Schematic", "who": "agent", "task": "Draw sch"},
                ],
            },
            "where": {
                "tracks": [{
                    "id": "board",
                    "position": pos,
                    "transitions": [{"seq": 1, "node": "inventory", "outcome": "pass", "target": pos,
                                     "actor": "script:baseline", "note": "ok", "at": "2026-11-27T12:00:00+00:00"}],
                }],
            },
            "state": {
                "tracks": {"board": {"position": pos}},
                "nodes": {
                    "inventory": {"status": "passed", "last": {"outcome": "pass", "actor": "script:baseline", "note": "ok"}},
                    "schematic": {"status": "current", "attempt": {"by": f"agent:{name}", "since": "now", "note": "working"}},
                },
            },
        }

    def test_hub_serves_http_and_local_project_sources_with_sse(self) -> None:
        remote_dir = self.root / "remote_view"
        remote_dir.mkdir()
        (remote_dir / "index.html").write_text("<p>child</p>", encoding="utf-8")
        (remote_dir / "view.json").write_text(json.dumps(self._make_sample_view("Claude", "schematic")), encoding="utf-8")
        child = board_graph_serve.server("127.0.0.1", 0, remote_dir)
        threading.Thread(target=child.serve_forever, daemon=True).start()
        self.addCleanup(child.server_close)
        self.addCleanup(child.shutdown)
        child_url = f"http://127.0.0.1:{child.server_address[1]}"

        local_proj = self.root / "local_proj"
        (local_proj / "tools" / "board_graph_view").mkdir(parents=True)
        (local_proj / "tools" / "board_graph_view" / "view.json").write_text(
            json.dumps(self._make_sample_view("LocalBoard", "inventory")), encoding="utf-8"
        )

        sources = [
            board_graph_hub.parse_source_spec(f"claude={child_url}"),
            board_graph_hub.parse_source_spec(f"local={local_proj}", is_project=True),
            board_graph_hub.parse_source_spec("down=http://127.0.0.1:9"),
        ]
        listeners = board_graph_hub.HubListeners()
        stop_event = threading.Event()
        board_graph_hub._start_source_watchers(sources, listeners, stop_event)
        hub = board_graph_hub.HubServer(("127.0.0.1", 0), sources, listeners)
        threading.Thread(target=hub.serve_forever, daemon=True).start()
        self.addCleanup(hub.server_close)
        self.addCleanup(hub.shutdown)
        self.addCleanup(stop_event.set)
        hub_url = f"http://127.0.0.1:{hub.server_port}"

        with urllib.request.urlopen(f"{hub_url}/", timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("board-graph Hub", resp.read().decode("utf-8"))

        with urllib.request.urlopen(f"{hub_url}/api/summary", timeout=5) as resp:
            summary = json.loads(resp.read().decode("utf-8"))
        by_name = {row["name"]: row for row in summary["sources"]}
        self.assertTrue(by_name["claude"]["online"])
        self.assertEqual(by_name["claude"]["position"], "schematic")
        self.assertEqual(by_name["claude"]["working"], "agent:Claude")
        self.assertEqual(by_name["claude"]["tool"]["ahead"], 1)
        self.assertTrue(by_name["local"]["online"])
        self.assertEqual(by_name["local"]["position"], "inventory")
        self.assertFalse(by_name["down"]["online"])

        with urllib.request.urlopen(f"{hub_url}/api/source/claude/view.json", timeout=5) as resp:
            proxied = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(proxied["project"], "Claude")

        stream = urllib.request.urlopen(f"{hub_url}/events", timeout=5)
        self.addCleanup(stream.close)
        first_line = stream.readline().decode("utf-8")
        self.assertTrue(first_line.startswith("data: "), first_line)

    def test_tool_status_reads_branch_and_ahead_commits(self) -> None:
        repo = self.root / "tool_repo"
        (repo / "tools").mkdir(parents=True)
        (repo / "tools" / "board_graph_tool.json").write_text('{"version": "0.34.0"}\n', encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "init", "-b", "main"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Tester"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@local.invalid"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "Base commit"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "checkout", "-b", "exp/test"], check=True, capture_output=True)
        (repo / "README.md").write_text("hi\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "Self heal fix"], check=True, capture_output=True)

        st = tool_paths.tool_status(repo)
        self.assertEqual(st["version"], "0.34.0")
        self.assertEqual(st["branch"], "exp/test")
        self.assertEqual(st["ahead"], 1)
        self.assertEqual(st["commits_ahead"][0]["subject"], "Self heal fix")

    def test_hub_subcommand_registered_in_cli(self) -> None:
        self.assertEqual(board_graph_cli.SUBCOMMANDS["hub"], "scripts/board_graph_hub.py")

    def test_hub_models_and_live_pcb_fallback_to_artifact_png(self) -> None:
        proj = self.root / "remote_proj"
        (proj / "board" / "reports").mkdir(parents=True)
        png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        (proj / "board" / "reports" / "front.png").write_bytes(png_bytes)
        view_dir = proj / "tools" / "board_graph_view"
        view_dir.mkdir(parents=True)
        (view_dir / "index.html").write_text("<p>v</p>", encoding="utf-8")
        view = self._make_sample_view("Alpha", "routing_batch")
        view["model"] = "model-from-view"
        view["artifacts"] = {
            "cards": {
                "routing_batch": {
                    "items": [{"path": "board/reports/front.png", "label": "Front copper", "exists": True, "kind": "image"}]
                }
            }
        }
        (view_dir / "view.json").write_text(json.dumps(view), encoding="utf-8")
        child = board_graph_serve.server("127.0.0.1", 0, view_dir, repo_root=proj)
        threading.Thread(target=child.serve_forever, daemon=True).start()
        self.addCleanup(child.server_close)
        self.addCleanup(child.shutdown)
        child_url = f"http://127.0.0.1:{child.server_address[1]}"

        args = board_graph_hub.parse_args(["--source", f"alpha={child_url}", "--model", "alpha=gpt-6.2-sol"])
        sources = board_graph_hub.load_sources(args)
        listeners = board_graph_hub.HubListeners()
        hub = board_graph_hub.HubServer(("127.0.0.1", 0), sources, listeners)
        threading.Thread(target=hub.serve_forever, daemon=True).start()
        self.addCleanup(hub.server_close)
        self.addCleanup(hub.shutdown)
        hub_url = f"http://127.0.0.1:{hub.server_port}"

        with urllib.request.urlopen(f"{hub_url}/api/summary", timeout=5) as resp:
            summary = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(summary["default_scope"], "auto")
        row = summary["sources"][0]
        self.assertEqual(row["model"], "gpt-6.2-sol")
        self.assertEqual(row["preview_images"][0]["path"], "board/reports/front.png")
        with urllib.request.urlopen(f"{hub_url}/api/source/alpha/live-pcb.png", timeout=5) as resp:
            self.assertEqual(resp.read(), png_bytes)
        with urllib.request.urlopen(f"{hub_url}/api/source/alpha/files/board/reports/front.png", timeout=5) as resp:
            self.assertEqual(resp.read(), png_bytes)
        (proj / "board" / "DECISIONS.md").write_text("# Board decisions\n\n- Keep 32 x 35 mm.\n",
                                                    encoding="utf-8", newline="\n")
        with urllib.request.urlopen(f"{hub_url}/api/source/alpha/files/board/DECISIONS.md?raw=1", timeout=5) as resp:
            self.assertEqual(resp.read().decode("utf-8"), "# Board decisions\n\n- Keep 32 x 35 mm.\n")

    def test_seed_exclusions_and_library_findings_when_exclusions_missing(self) -> None:
        import types
        import board_graph_actions
        (self.root / "board").mkdir()
        (self.root / "board" / "board.json").write_text(
            '{"drc_exclusions": "board/reports/documented-drc-exclusions.json"}\n', encoding="utf-8"
        )
        board_graph_cli._seed_exclusions_file(self.root)
        seeded = self.root / "board" / "reports" / "documented-drc-exclusions.json"
        self.assertTrue(seeded.is_file())
        self.assertEqual(json.loads(seeded.read_text(encoding="utf-8")), {"accepted_warning_refs": []})

        other = self.root / "no_excl"
        (other / "board").mkdir(parents=True)
        (other / "board" / "fp-lib-table").write_text("(fp_lib_table)\n", encoding="utf-8")
        proj = types.SimpleNamespace(
            root=other,
            project_file={"board": {"design_settings": {}}},
            fps={},
            library={},
        )
        findings, detail = board_graph_actions.library_findings(proj)
        self.assertFalse(any("documented-drc-exclusions.json" in f for f in findings), findings)
        self.assertEqual(detail["accepted_mismatches"], [])

    def test_hub_timing_and_started_at_override_plus_view_run_timing(self) -> None:
        import types
        import board_graph_view

        view_dir = self.root / "timed_view"
        view_dir.mkdir()
        (view_dir / "index.html").write_text("<p>t</p>", encoding="utf-8")
        v_done = self._make_sample_view("CodexDone", "quote")
        v_done["autonomy"] = {"mode": "delegated", "until": "quote_files"}
        v_done["where"]["tracks"][0]["transitions"] = [
            {"seq": 15, "node": "quote_files", "outcome": "pass", "target": "quote",
             "actor": "agent:codex", "note": "ok", "at": "2026-11-27T20:06:05+00:00"}
        ]
        (view_dir / "view.json").write_text(json.dumps(v_done), encoding="utf-8")
        child = board_graph_serve.server("127.0.0.1", 0, view_dir)
        threading.Thread(target=child.serve_forever, daemon=True).start()
        self.addCleanup(child.server_close)
        self.addCleanup(child.shutdown)

        running_proj = self.root / "running_proj"
        (running_proj / "tools" / "board_graph_view").mkdir(parents=True)
        v_run = self._make_sample_view("ClaudeRun", "routing_batch")
        (running_proj / "tools" / "board_graph_view" / "view.json").write_text(json.dumps(v_run), encoding="utf-8")

        args = board_graph_hub.parse_args([
            "--source", f"codex=http://127.0.0.1:{child.server_address[1]}",
            "--project", f"claude={running_proj}",
            "--started-at", "codex=2026-11-27T19:25:54+00:00",
        ])
        sources = board_graph_hub.load_sources(args)
        hub = board_graph_hub.HubServer(("127.0.0.1", 0), sources, board_graph_hub.HubListeners())
        threading.Thread(target=hub.serve_forever, daemon=True).start()
        self.addCleanup(hub.server_close)
        self.addCleanup(hub.shutdown)
        with urllib.request.urlopen(f"http://127.0.0.1:{hub.server_port}/api/summary", timeout=5) as resp:
            by_name = {r["name"]: r for r in json.loads(resp.read().decode("utf-8"))["sources"]}
        self.assertEqual(by_name["codex"]["timing"], {
            "started_at": "2026-11-27T19:25:54+00:00",
            "finished_at": "2026-11-27T20:06:05+00:00",
            "running": False,
        })
        self.assertTrue(by_name["claude"]["timing"]["running"])
        self.assertEqual(by_name["claude"]["timing"]["started_at"], "2026-11-27T12:00:00+00:00")

        fake_graph = types.SimpleNamespace(
            data={"autonomy": {"mode": "delegated", "until": "quote_files",
                               "authorized_by": "owner", "reason": "test"}},
            tracks={"board": {}},
        )
        fake_state = types.SimpleNamespace(
            transitions=lambda track: [
                {"node": "inventory", "outcome": "pass", "at": "2026-11-27T19:25:55+00:00"},
                {"node": "quote_files", "outcome": "pass", "at": "2026-11-27T20:05:00+00:00"},
            ],
            attempts=lambda: [{"started_at": "2026-11-27T19:25:54+00:00"}],
            position=lambda g, t: "quote",
        )
        rt = board_graph_view.run_timing(fake_graph, fake_state)
        self.assertEqual(rt, {"started_at": "2026-11-27T19:25:54+00:00",
                              "finished_at": "2026-11-27T20:05:00+00:00", "running": False})

    def test_inline_scripts_pass_js_syntax_check(self) -> None:
        import re
        import shutil

        node = shutil.which("node")
        if not node:
            self.skipTest("node not on PATH")
        index_html = (REPO_ROOT / "tools" / "board_graph_view" / "index.html").read_text(encoding="utf-8")
        for label, html in (("hub", board_graph_hub.HUB_HTML), ("index", index_html)):
            match = re.search(r"<script>(.*?)</script>", html, re.S)
            self.assertIsNotNone(match, f"missing <script> in {label}")
            js_file = self.root / f"{label}.js"
            js_file.write_text(match.group(1), encoding="utf-8")
            done = subprocess.run([node, "--check", str(js_file)], capture_output=True, text=True, check=False)
            self.assertEqual(done.returncode, 0, f"{label} JS syntax error:\n{done.stderr}")


if __name__ == "__main__":
    unittest.main()
