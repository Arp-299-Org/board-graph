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


if __name__ == "__main__":
    unittest.main()
