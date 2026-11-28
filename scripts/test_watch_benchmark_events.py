#!/usr/bin/env python3
"""Unit tests for docker/watch_benchmark_events.py SSE resilience and tool_heal awareness."""
from __future__ import annotations

import io
import json
import pathlib
import queue
import sys
import threading
import unittest
from unittest import mock

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCKER_DIR = REPO_ROOT / "docker"
if str(DOCKER_DIR) not in sys.path:
    sys.path.insert(0, str(DOCKER_DIR))

import watch_benchmark_events as watcher  # noqa: E402


class _FakeResponse:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args) -> None:
        return None


class WatchBenchmarkEventsTests(unittest.TestCase):
    def test_board_state_extracts_board_and_tool_heal_fields(self) -> None:
        view = {
            "project": "ble-62",
            "where": {
                "tracks": [{
                    "id": "board",
                    "position": "routing_batch",
                    "transitions": [{
                        "seq": 14,
                        "node": "routing_batch",
                        "outcome": "more_work",
                        "target": "routing_batch",
                        "actor": "agent:codex",
                        "note": "batch 1",
                    }],
                }],
            },
            "tool_heal": {
                "version": "fix-drc",
                "position": "implement",
                "done": False,
                "blocked_outcome": "needs_lead",
                "blocked_node": "tool_checks",
                "gate_ok": False,
                "gate_reason": "uncommitted changes",
            },
        }
        st = watcher._board_state(view)
        self.assertEqual(st["project"], "ble-62")
        self.assertEqual(st["seq"], 14)
        self.assertEqual(st["heal_version"], "fix-drc")
        self.assertEqual(st["heal_position"], "implement")
        self.assertEqual(st["heal_blocked_outcome"], "needs_lead")
        self.assertEqual(st["heal_blocked_node"], "tool_checks")
        self.assertFalse(st["heal_gate_ok"])

    def test_watch_target_survives_multiple_sse_drops_and_catches_needs_lead(self) -> None:
        views = [
            {"project": "ble", "where": {"tracks": [{"id": "board", "position": "schematic", "transitions": [{"seq": 3, "node": "datasheets", "outcome": "pass", "target": "schematic", "actor": "agent:a"}]}]}},
            {"project": "ble", "where": {"tracks": [{"id": "board", "position": "schematic", "transitions": [{"seq": 3, "node": "datasheets", "outcome": "pass", "target": "schematic", "actor": "agent:a"}]}]}},
            {"project": "ble", "where": {"tracks": [{"id": "board", "position": "schematic", "transitions": [{"seq": 4, "node": "schematic", "outcome": "needs_lead", "target": "schematic", "actor": "agent:a", "note": "bug"}]}]}},
        ]
        stream_calls = 0

        def fake_urlopen(req, timeout=None):
            nonlocal stream_calls
            stream_calls += 1
            if stream_calls in (1, 2, 4):
                raise ConnectionError(f"transient drop #{stream_calls}")
            if stream_calls == 3:
                return _FakeResponse([b": keepalive\n", b""])
            return _FakeResponse([b"data: {}\n"])

        events_q: queue.Queue = queue.Queue()
        with mock.patch.object(watcher.urllib.request, "urlopen", side_effect=fake_urlopen), \
                mock.patch.object(watcher, "_fetch_view", side_effect=views), \
                mock.patch.object(watcher.time, "sleep"), \
                mock.patch.object(sys, "stdout", new=io.StringIO()):
            watcher._watch_target(
                "vm1", "http://127.0.0.1:8001", "quote_files",
                {"needs_owner", "needs_lead", "needs_skill"}, 0, False,
                events_q, threading.Lock(), set(), {}, max_retries=3,
            )
        kind, code, name, detail = events_q.get_nowait()
        self.assertEqual((kind, code, name), ("blocked", 10, "vm1"))
        self.assertEqual(detail["seq"], 4)

    def test_watch_target_triggers_heal_blocked_immediately(self) -> None:
        view = {
            "project": "ble",
            "where": {"tracks": [{"id": "board", "position": "routing_batch", "transitions": [{"seq": 5, "node": "placement", "outcome": "pass", "target": "routing_batch", "actor": "agent:a"}]}]},
            "tool_heal": {
                "version": "fix-ipc",
                "position": "tool_checks",
                "done": False,
                "blocked_outcome": "needs_lead",
                "blocked_node": "tool_checks",
                "gate_ok": False,
            },
        }
        events_q: queue.Queue = queue.Queue()
        with mock.patch.object(watcher.urllib.request, "urlopen", return_value=_FakeResponse([])), \
                mock.patch.object(watcher, "_fetch_view", return_value=view), \
                mock.patch.object(sys, "stdout", new=io.StringIO()):
            watcher._watch_target(
                "vm2", "http://127.0.0.1:8002", "quote_files",
                {"needs_owner", "needs_lead", "needs_skill"}, 0, False,
                events_q, threading.Lock(), set(), {},
            )
        kind, code, name, detail = events_q.get_nowait()
        self.assertEqual((kind, code, name), ("heal_blocked", 12, "vm2"))
        self.assertEqual(detail["heal_blocked_node"], "tool_checks")


if __name__ == "__main__":
    unittest.main()
