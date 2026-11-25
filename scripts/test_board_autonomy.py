#!/usr/bin/env python3
"""Unit and workflow tests for explicit autonomous delegation mode and the hard order ceiling."""
from __future__ import annotations

import hashlib
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import board_approvals  # noqa: E402
import board_autonomy  # noqa: E402
import board_graph as bg  # noqa: E402


def _sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_graph_data(autonomy: dict | None = None) -> dict:
    data = {
        "version": 2,
        "approval_mode": "enforce",
        "milestone": "Test board",
        "docs": [],
        "stay_outcomes": {
            "more_work": "stay",
            "needs_owner": "stay",
            "needs_lead": "stay",
            "needs_skill": "stay",
        },
        "move_outcomes": {"pass": "next", "returned": "back"},
        "tracks": [{"id": "board", "label": "Board", "start": "decisions", "ordered": True}],
        "stages": [{"id": "build", "label": "Build"}],
        "nodes": [
            {
                "id": "decisions",
                "track": "board",
                "stage": "build",
                "label": "Decisions",
                "who": "owner",
                "task": "Record decisions",
                "pass_when": "Recorded",
                "owns": ["board/DECISIONS.md"],
                "action": None,
                "approval_owned": True,
                "outcomes": {"pass": "placement", "returned": []},
            },
            {
                "id": "placement",
                "track": "board",
                "stage": "build",
                "label": "Placement",
                "who": "owner",
                "task": "Approve placement",
                "pass_when": "Rendered",
                "owns": ["board/owner_placement.json"],
                "action": None,
                "presentation": "board_render",
                "requires_current": ["decisions"],
                "unchanged_pass": "placement",
                "outcomes": {"pass": "quote_files", "returned": ["decisions"]},
            },
            {
                "id": "quote_files",
                "track": "board",
                "stage": "build",
                "label": "Quote files",
                "who": "agent",
                "task": "Prepare quotes",
                "pass_when": "Ready",
                "owns": [],
                "action": None,
                "approval_owned": True,
                "requires_current": ["placement"],
                "outcomes": {"pass": "order", "returned": ["placement"]},
            },
            {
                "id": "order",
                "track": "board",
                "stage": "build",
                "label": "Order",
                "who": "owner",
                "task": "Authorize order",
                "pass_when": "Authorized",
                "owns": [],
                "action": None,
                "requires_current": ["quote_files"],
                "outcomes": {"pass": "done", "returned": ["quote_files"]},
            },
        ],
        "open_issues": [],
    }
    if autonomy is not None:
        data["autonomy"] = autonomy
    return data


def _write_project(root: pathlib.Path, autonomy: dict | None = None) -> tuple[pathlib.Path, bg.Graph]:
    (root / "tools").mkdir(parents=True, exist_ok=True)
    (root / "board").mkdir(parents=True, exist_ok=True)
    (root / "review").mkdir(parents=True, exist_ok=True)
    (root / "board" / "DECISIONS.md").write_text("# Decisions\nSpec v1\n", encoding="utf-8")
    (root / "board" / "owner_placement.json").write_text("{}\n", encoding="utf-8")
    (root / "board" / "example.kicad_pcb").write_text("(kicad_pcb (version 20241229))\n", encoding="utf-8")
    (root / "quotes.txt").write_text("Quote summary\n", encoding="utf-8")
    (root / "order.txt").write_text("PO #1\n", encoding="utf-8")
    graph_path = root / "tools" / "board_graph.json"
    data = _make_graph_data(autonomy)
    graph_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return graph_path, bg.Graph(graph_path)


def _write_render(root: pathlib.Path) -> list[str]:
    pcb = root / "board" / "example.kicad_pcb"
    top = root / "review" / "top.png"
    top.write_bytes(b"\x89PNG\r\n\x1a\nfakerender")
    receipt = {
        "schema": "board-graph/board-render/v2",
        "board_sha256": _sha(pcb),
        "images": {"top.png": _sha(top)},
        "models": {},
    }
    (root / "review" / "board-render.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return ["review/board-render.json", "review/top.png"]


class BoardAutonomyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._td.name)
        self.views = {"placement": lambda root: "placement-view-v1"}
        self.sources = {"render_models": lambda root: {"models": {}}}

    def tearDown(self) -> None:
        self._td.cleanup()

    def _mock_env(self):
        return (
            mock.patch.object(bg, "VIEW_FILE", "board/example.kicad_pcb"),
            mock.patch.object(bg, "PACK", mock.Mock(
                approval_sources=self.sources,
                release_checks={},
                legacy_release={},
                legacy_pass={},
                gates={},
            )),
            mock.patch.object(bg, "VIEWS", {"placement": (self.views["placement"], "placement")}),
        )

    def test_starter_graph_rejects_delegating_order_and_requires_owner_auth(self) -> None:
        starter_data = json.loads((REPO_ROOT / "tools" / "board_graph.json").read_text(encoding="utf-8"))
        for bad_until in ("order", "delivery", "first_power"):
            data = json.loads(json.dumps(starter_data))
            data["autonomy"] = {"mode": "delegated", "until": bad_until, "authorized_by": "owner", "reason": "bench test"}
            with self.assertRaisesRegex(ValueError, "order always requires owner"):
                bg.Graph(REPO_ROOT / "tools" / "board_graph.json", data=data)

        missing_auth = json.loads(json.dumps(starter_data))
        missing_auth["autonomy"] = {"mode": "delegated", "until": "quote_files", "authorized_by": "agent:bot", "reason": "r"}
        with self.assertRaisesRegex(ValueError, "authorized_by must be 'owner'"):
            bg.Graph(REPO_ROOT / "tools" / "board_graph.json", data=missing_auth)

        valid = json.loads(json.dumps(starter_data))
        valid["autonomy"] = {"mode": "delegated", "until": "quote_files", "authorized_by": "owner", "reason": "test run"}
        graph = bg.Graph(REPO_ROOT / "tools" / "board_graph.json", data=valid)
        self.assertTrue(board_autonomy.is_delegated(graph, "decisions"))
        self.assertTrue(board_autonomy.is_delegated(graph, "placement"))
        self.assertFalse(board_autonomy.is_delegated(graph, "order"))

    def test_delegated_walk_enforces_presentation_stamps_audit_and_blocks_order(self) -> None:
        p1, p2, p3 = self._mock_env()
        with p1, p2, p3:
            graph_path, graph = _write_project(
                self.root,
                {"mode": "delegated", "until": "quote_files", "authorized_by": "owner", "reason": "Autonomous test"},
            )
            state = bg.State(self.root / "state.sqlite3", self.root / "records")

            dec = bg.record(graph, state, "decisions", "pass", "agent:bot", "Delegated decisions", ["board/DECISIONS.md"],
                            root=self.root, views=self.views)
            self.assertEqual(dec["actor"], "agent:bot")
            self.assertIs(dec["inputs"]["delegated"], True)

            with self.assertRaisesRegex(ValueError, "Cite the preview images"):
                bg.record(graph, state, "placement", "pass", "agent:bot", "Missing render", ["board/owner_placement.json"],
                          root=self.root, views=self.views)

            render_files = _write_render(self.root)
            plc = bg.record(graph, state, "placement", "pass", "agent:bot", "Delegated placement", render_files,
                            root=self.root, views=self.views)
            self.assertIs(plc["inputs"]["delegated"], True)
            self.assertIn("approved_presentation", plc["inputs"])

            bg.record(graph, state, "quote_files", "pass", "agent:bot", "Quotes ready", ["quotes.txt"],
                      root=self.root, views=self.views)

            view = bg.where(graph, state)
            rendered = bg.render(view)
            self.assertIn("Mode: AUTONOMOUS (delegated through quote_files; order always requires owner)", rendered)
            brief_text = bg.brief(graph, state, "decisions", root=self.root)
            self.assertIn("Autonomy: delegated through quote_files", brief_text)

            with self.assertRaisesRegex(ValueError, "never be delegated"):
                bg.record(graph, state, "order", "pass", "agent:bot", "Unauthorized order", ["order.txt"],
                          root=self.root, views=self.views)

            with self.assertRaisesRegex(ValueError, "delegated approvals: decisions, placement"):
                bg.record(graph, state, "order", "pass", "owner", "Owner order before re-approval", ["order.txt"],
                          root=self.root, views=self.views)

            # Switching back to supervised invalidates delegated approvals for downstream cards.
            sup_graph = bg.Graph(graph_path, data=_make_graph_data({"mode": "supervised"}))
            rows = board_approvals.read_records(self.root / "records")
            rev = board_approvals.Approvals(sup_graph, rows, self.root, self.views, bg.file_sha256, self.sources,
                                          evidence_hash=bg.hash_path)
            self.assertEqual(rev.verdict("decisions")["status"], "unproven")
            self.assertEqual(rev.verdict("placement")["status"], "unproven")


if __name__ == "__main__":
    unittest.main()
