#!/usr/bin/env python3
"""Contract tests for enforced 6-card self-healing (`board-graph heal`) and Hub/routing view sync."""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import board_graph_cli  # noqa: E402
import board_heal  # noqa: E402
import editor_step_prompts  # noqa: E402
import routing_run  # noqa: E402


def _init_fake_tool_repo(root: pathlib.Path) -> pathlib.Path:
    tool = root / "tool"
    for folder in ("dev", "scripts", "tools"):
        (tool / folder).mkdir(parents=True, exist_ok=True)
    for rel in ("dev/tool_dev.py", "dev/tool_graph.json", "dev/tool_pack.json", "dev/tool_actions.py"):
        (tool / rel).write_text((REPO_ROOT / rel).read_text(encoding="utf-8"), encoding="utf-8")
    (tool / "scripts" / "board_graph.py").write_text("# stub\n", encoding="utf-8")
    (tool / "tools" / "board_graph_tool.json").write_text('{"version": "0.42.0", "files": {}, "starter": []}\n', encoding="utf-8")
    (tool / "pyproject.toml").write_text('[project]\nname = "board-graph"\nversion = "0.42.0"\n', encoding="utf-8")
    (tool / "UPGRADING.md").write_text("# Upgrading\n\n## 0.42.0\n\nNotes.\n", encoding="utf-8")
    for cmd in (
        ["git", "init", "-b", "main"],
        ["git", "config", "user.name", "Test"],
        ["git", "config", "user.email", "t@local.invalid"],
        ["git", "add", "."],
        ["git", "commit", "-q", "-m", "Base"],
    ):
        subprocess.run(cmd, cwd=str(tool), check=True, capture_output=True, text=True)
    subprocess.run(["git", "update-ref", "refs/remotes/origin/main", "HEAD"], cwd=str(tool), check=True, capture_output=True)
    subprocess.run(["git", "checkout", "-q", "-b", "exp/fix"], cwd=str(tool), check=True, capture_output=True)
    return tool


class BoardHealTests(unittest.TestCase):
    def test_heal_subcommand_registered_and_reviewer_warns_against_pad_center_false_stubs(self) -> None:
        self.assertEqual(board_graph_cli.SUBCOMMANDS["heal"], "scripts/board_heal.py")
        self.assertIn("verified same-net pad center", editor_step_prompts.REVIEW)

    def test_gate_blocks_unverified_tool_edits_until_six_cards_pass(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            tool = _init_fake_tool_repo(root)
            board_graph = root / "proj" / "tools" / "board_graph.json"
            board_graph.parent.mkdir(parents=True)
            board_graph.write_text('{"nodes": []}\n', encoding="utf-8")

            # Clean checkout on exp/fix matching origin/main passes Gate 1.
            self.assertIsNone(board_heal.check_tool_heal_gate(tool, graph_path=board_graph, command="next", bypass_test_skip=True))

            # Editing a script leaves uncommitted changes -> blocked on next, allowed on record needs_lead & dev/tool_graph.json.
            (tool / "scripts" / "board_graph.py").write_text("# edited\n", encoding="utf-8")
            blocked = board_heal.check_tool_heal_gate(tool, graph_path=board_graph, command="next", bypass_test_skip=True)
            self.assertIsNotNone(blocked)
            self.assertIn("Self-healing gate blocked", blocked)
            self.assertIsNone(
                board_heal.check_tool_heal_gate(tool, graph_path=board_graph, command="record", outcome="needs_lead", bypass_test_skip=True)
            )
            self.assertIsNone(
                board_heal.check_tool_heal_gate(tool, graph_path=tool / "dev" / "tool_graph.json", command="next", bypass_test_skip=True)
            )

            # Commit tool edit cleanly, then simulate completed 6-card heal cycle matching code digest.
            subprocess.run(["git", "-C", str(tool), "add", "scripts/board_graph.py"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(tool), "commit", "-q", "-m", "Fix bug"], check=True, capture_output=True)
            still_blocked = board_heal.check_tool_heal_gate(tool, graph_path=board_graph, command="next", bypass_test_skip=True)
            self.assertIsNotNone(still_blocked)

            cur_digest = board_heal.tool_code_hash(tool)
            fake_cards = [{"id": n, "label": n, "status": "passed"} for n in ("triage", "implement", "tool_checks", "cold_review", "live_smoke", "release")]
            done_summary = {
                "available": True, "version": "fix-bug", "mode": "heal", "position": "done", "done": True,
                "passed_count": 6, "total_count": 6, "dirty": False, "ahead": 1, "code_digest": cur_digest,
                "verified_digest": cur_digest, "tool_code_current": True, "gate_ok": True, "gate_reason": None,
                "cards": fake_cards,
            }
            with mock.patch.object(board_heal, "tool_heal_summary", return_value=done_summary):
                self.assertIsNone(board_heal.check_tool_heal_gate(tool, graph_path=board_graph, command="next", bypass_test_skip=True))

    def test_needs_lead_hint_and_routing_engine_attaches_view_follow(self) -> None:
        hint = board_heal.needs_lead_hint({"tracks": [{"node": {"id": "routing_batch", "last": {"outcome": "needs_lead"}}}]})
        self.assertIsNotNone(hint)
        self.assertIn("board-graph heal start fix-routing_batch --from-card routing_batch", hint)

        aut_hint = board_heal.needs_lead_hint({
            "autonomy": {"mode": "delegated"},
            "tracks": [{"node": {"id": "routing_batch", "last": {"outcome": "needs_lead"}}}],
        })
        self.assertIsNotNone(aut_hint)
        self.assertIn("do not exit waiting on an external lead", aut_hint)

        fake_state = mock.MagicMock()
        fake_where = {"path": pathlib.Path("state.sqlite3"), "how": "local", "view": pathlib.Path("tools/board_graph_view")}
        with mock.patch.object(routing_run.board_graph, "Graph", return_value="G"), \
                mock.patch.object(routing_run.board_graph, "place", return_value=fake_where), \
                mock.patch.object(routing_run.board_graph, "open_state", return_value=fake_state), \
                mock.patch.object(routing_run.board_graph, "follow") as follow_mock:
            routing_run.engine(mock.MagicMock(graph=pathlib.Path("tools/board_graph.json"), state=None, records=None))
        follow_mock.assert_called_once_with("G", fake_state, fake_where["view"], root=routing_run.ROOT)

    def test_heal_mode_rejects_env_bypass_guides_manifest_and_syncs_agents(self) -> None:
        import board_graph_tool
        sys.path.insert(0, str(REPO_ROOT / "dev"))
        import tool_actions

        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            tool = _init_fake_tool_repo(root)
            for rel in ("scripts/board_graph_tool.py", "scripts/no_window.py", ".gitignore"):
                (tool / rel).write_text((REPO_ROOT / rel).read_text(encoding="utf-8"), encoding="utf-8")
            (tool / "tools" / "board_graph_tool.json").unlink()
            board_graph_tool.manifest(tool, "0.42.0", add=("scripts/board_graph.py",))
            (tool / "AGENTS.md").write_text(
                "# Agent instructions\n\n## Product\n\nTemplate.\n\n## The owner\n\nUpdated rule v2.\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "-C", str(tool), "add", "-A"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(tool), "commit", "-q", "-m", "Non-empty manifest"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(tool), "update-ref", "refs/remotes/origin/main", "HEAD"], check=True, capture_output=True)

            proj = root / "proj"
            (proj / "tools").mkdir(parents=True)
            board_graph = proj / "tools" / "board_graph.json"
            board_graph.write_text('{"nodes": []}\n', encoding="utf-8")
            (proj / "AGENTS.md").write_text(
                "# Agent instructions\n\n## Product\n\nBLE 6.2 sensor node.\n\n## The owner\n\nStale rule v1.\n",
                encoding="utf-8",
            )

            # Env var must NOT bypass the self-healing gate on a real board repo.
            self.assertFalse(hasattr(board_heal, "SKIP_GATE_ENV"))
            (tool / "scripts" / "board_graph.py").write_text("# edited\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"BOARD_GRAPH_SKIP_HEAL_GATE": "1"}):
                blocked = board_heal.check_tool_heal_gate(
                    tool, graph_path=board_graph, command="next", bypass_test_skip=True
                )
            self.assertIsNotNone(blocked)
            self.assertIn("Self-healing gate blocked", blocked)

            # Start heal cycle linked to proj; commit edit WITHOUT refreshing manifest -> heal hint returned.
            (tool / "logs" / "tool-dev").mkdir(parents=True, exist_ok=True)
            (tool / "logs" / "tool-dev" / "current.json").write_text(
                json.dumps({"version": "fix-ble", "mode": "heal", "board_project": str(proj)}),
                encoding="utf-8",
            )
            subprocess.run(["git", "-C", str(tool), "add", "scripts/board_graph.py"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(tool), "commit", "-q", "-m", "Fix script without manifest"], check=True, capture_output=True)
            res_bad = tool_actions.run_release_gate(tool)
            self.assertEqual(res_bad["outcome"], "needs_lead")
            self.assertTrue(
                any("heal mode: run 'python scripts/board_graph_tool.py manifest --version 0.42.0'" in f for f in res_bad["findings"]),
                res_bad["findings"],
            )

            # Refresh manifest and commit -> release_gate passes.
            board_graph_tool.manifest(tool, "0.42.0")
            subprocess.run(["git", "-C", str(tool), "add", "tools/board_graph_tool.json"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(tool), "commit", "-q", "-m", "Refresh manifest"], check=True, capture_output=True)
            res_ok = tool_actions.run_release_gate(tool)
            self.assertEqual(res_ok["outcome"], "pass", res_ok["findings"])

            # Completing heal propagates updated AGENTS.md rules to proj while preserving ## Product.
            with mock.patch.object(board_heal, "tool_heal_summary", return_value={"done": True, "board_project": str(proj)}), \
                    mock.patch.object(board_graph_tool, "ROOT", tool):
                board_heal._sync_board_project_agents(tool, proj)
            synced = (proj / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn("BLE 6.2 sensor node.", synced)
            self.assertIn("Updated rule v2.", synced)
            self.assertNotIn("Stale rule v1.", synced)


if __name__ == "__main__":
    unittest.main()
