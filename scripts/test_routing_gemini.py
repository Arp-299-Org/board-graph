"""Tests for scripts/routing_gemini.py: agy preflight, strict permissions, metering, and shell call auditing."""
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import routing_gemini as routing


class GeminiRoutingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name).resolve()
        self.log = self.root / "events.jsonl"
        self.model = "gemini-4-neon-high"
        self.board = self.root / "motor.kicad_pcb"
        self.board.write_text("(kicad_pcb)", encoding="utf-8")
        self.img_a = self.root / "start-top.png"
        self.img_b = self.root / "round1-top.png"
        self.img_a.write_bytes(b"\x89PNG\r\n\x1a\n")
        self.img_b.write_bytes(b"\x89PNG\r\n\x1a\n")
        self.env = {
            "HOME": str(self.root),
            "PATH": "/usr/bin:/bin",
            "SSH_CONNECTION": "10.0.0.1 1 10.0.0.2 22",
            "BOARD": str(self.board),
            "SOCKET": "ipc:///tmp/kicad/api-123.sock",
            "ROUTING_HANDOFF_AT": "12345",
        }

    def test_preflight_checks_agy_binary_fallback_and_required_flags(self):
        help_ok = "Usage: agy\n  --print --output-format --model --effort --settings --disable-slash-commands --print-timeout\n"
        with patch.object(routing.shutil, "which", return_value="/opt/bin/agy"), \
                patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout=help_ok, stderr="")):
            self.assertTrue(routing.preflight().endswith("agy"))
        fake_home = self.root / "home"
        fallback = fake_home / ".local" / "bin" / "agy"
        fallback.parent.mkdir(parents=True)
        fallback.write_text("#!/bin/sh\n", encoding="utf-8")
        with patch.object(routing.shutil, "which", return_value=None), \
                patch.object(routing.pathlib.Path, "home", return_value=fake_home), \
                patch.object(routing.os, "access", return_value=True), \
                patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout=help_ok, stderr="")):
            self.assertEqual(routing.preflight(), str(fallback.resolve()))
        with patch.object(routing.shutil, "which", return_value=None), \
                patch.object(routing.pathlib.Path, "home", return_value=self.root / "empty"):
            with self.assertRaisesRegex(ValueError, "was not found"):
                routing.preflight()
        with patch.object(routing.shutil, "which", return_value="/opt/bin/agy"), \
                patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="--print", stderr="")):
            with self.assertRaisesRegex(ValueError, "--output-format"):
                routing.preflight()

    def test_settings_lock_down_reviewer_and_allow_operator_shell_without_writes(self):
        op_settings, _ = routing._settings([self.img_a], operator=True)
        self.assertEqual(op_settings["permissions"]["allow"], ["command(*)", "read_file(*)"])
        for denied in ("write_file(*)", "edit_file(*)", "mcp(*)", "web(*)", "browser(*)", "subagent(*)"):
            self.assertIn(denied, op_settings["permissions"]["deny"])
        rev_settings, allowed = routing._settings([self.img_a, self.img_b], operator=False)
        self.assertEqual(allowed, {self.img_a.resolve(), self.img_b.resolve()})
        self.assertEqual(rev_settings["permissions"]["allow"],
                         [f"read_file({self.img_a.resolve()})", f"read_file({self.img_b.resolve()})"])
        self.assertIn("command(*)", rev_settings["permissions"]["deny"])

    def test_argv_validation(self):
        argv = routing._argv("/opt/bin/agy", self.model, "high", str(self.board), 42, self.root / "s.json", 30.2, "Route it")
        self.assertIn("--print", argv)
        self.assertIn("stream-json", argv)
        self.assertIn("--disable-slash-commands", argv)
        self.assertIn("31s", argv)
        # Effort-suffixed model IDs (e.g. gemini-4-neon-high) must omit --effort even when another effort is passed.
        self.assertNotIn("--effort", argv)
        argv_low = routing._argv("/opt/bin/agy", self.model, "low", str(self.board), None, self.root / "s.json", 10, "Review")
        self.assertNotIn("--effort", argv_low)
        argv_unsuffixed = routing._argv("/opt/bin/agy", "gemini-pro-orchestrator-neon", "high", str(self.board), 42,
                                        self.root / "s.json", 30, "Route it")
        self.assertEqual(argv_unsuffixed[argv_unsuffixed.index("--effort"):argv_unsuffixed.index("--effort") + 2],
                         ["--effort", "high"])
        opus_argv = routing._argv("/opt/bin/agy", "claude-opus-5-5", None, str(self.board), None, self.root / "s.json", 10, "x")
        self.assertIn("claude-opus-5-5", opus_argv)
        for bad_model in ("", "bad model"):
            with self.assertRaises(ValueError):
                routing._argv("/opt/bin/agy", bad_model, None, str(self.board), None, self.root / "s.json", 10, "x")
        with self.assertRaises(ValueError):
            routing._argv("/opt/bin/agy", self.model, None, "relative.kicad_pcb", 42, self.root / "s.json", 10, "x")

    def fake_popen(self, events, exitcode=0, *, operator=True, expected_prompt=None):
        test = self

        class Child:
            def __init__(child, argv, **kwargs):
                child.argv = argv
                test.last_argv = argv
                if expected_prompt is not None:
                    self.assertEqual(argv[argv.index("--print") + 1], expected_prompt)
                settings_file = pathlib.Path(argv[argv.index("--settings") + 1])
                test.last_settings = json.loads(settings_file.read_text(encoding="utf-8"))
                if operator:
                    test.assertEqual(kwargs["cwd"], test.root)
                    test.assertEqual(kwargs["env"]["SOCKET"], "ipc:///tmp/kicad/api-123.sock")
                    test.assertEqual(kwargs["env"]["ROUTING_HANDOFF_AT"], "12345")
                else:
                    test.assertNotEqual(kwargs["cwd"], test.root)
                    test.assertNotIn("SOCKET", kwargs["env"])
                    test.assertIn("SSH_CONNECTION", kwargs["env"])
                for event in events:
                    kwargs["stdout"].write((json.dumps(event) + "\n").encode("utf-8"))
                child.returncode = exitcode

            def wait(child, timeout=None):
                return child.returncode

        return patch.object(subprocess, "Popen", Child)

    def ok_events(self, *, response="NOTHING.", model=None, input_tokens=15339, cached=10035, output=95, thinking=12):
        return [
            {"event": "init", "conversation_id": "c1", "init": {"model": model or self.model, "cwd": str(self.root)}},
            {"event": "step_update", "step_update": {"step_index": 1, "state": "DONE", "step_type": "tool",
                                                     "tool_name": "read_file",
                                                     "tool_info": {"name": "read_file", "parameters": {"Path": str(self.img_a)}}}},
            {"event": "result", "result": {"conversation_id": "c1", "status": "SUCCESS", "response": response,
                                           "num_turns": 2,
                                           "usage": {"input_tokens": input_tokens, "output_tokens": output,
                                                     "thinking_tokens": thinking, "cache_read_tokens": cached,
                                                     "total_tokens": input_tokens + output}}},
        ]

    def test_metering_preserves_agy_non_cached_input_tokens(self):
        events = self.ok_events(input_tokens=15339, cached=10035, output=95, thinking=12)
        with self.fake_popen(events, operator=False):
            ok, answer = routing.run("/opt/bin/agy", "Review", self.model, "high", self.root, self.env,
                                     self.log, 60, images=[self.img_a, self.img_b])
        self.assertEqual((ok, answer), (True, "NOTHING."))
        usage = routing.tokens(self.log, self.model)
        self.assertTrue(usage["complete"])
        self.assertEqual(usage["rounds"][0]["turns"], 2)
        self.assertEqual(usage["by_model"][self.model],
                         {"input": 15339, "output": 95, "thinking": 12, "cache_read": 10035, "cache_write": None})

    def test_large_prompt_spills_to_allowlisted_prompt_file(self):
        big_prompt = "P" * (routing.MAX_INLINE_PROMPT_BYTES + 100)
        with self.fake_popen(self.ok_events(), operator=False):
            ok, _ = routing.run("/opt/bin/agy", big_prompt, self.model, None, self.root, self.env,
                                self.log, 60, images=[self.img_a])
        self.assertTrue(ok)
        printed = self.last_argv[self.last_argv.index("--print") + 1]
        prompt_rules = [rule for rule in self.last_settings["permissions"]["allow"]
                        if rule.startswith("read_file(") and "prompt.txt)" in rule]
        self.assertEqual(len(prompt_rules), 1)
        allowed_prompt_path = prompt_rules[0][len("read_file("):-1]
        self.assertIn(allowed_prompt_path, printed)

    def test_shell_call_auditing_parses_agy_run_command_events(self):
        def cmd_event(cmd, body, code=0, state="DONE"):
            out = f"Created At: t\nCompleted At: t\nCommand exited with code {code}.\n\nCommand output:\n{body}"
            return {"event": "step_update", "step_update": {
                "step_index": 2, "state": state, "step_type": "tool", "tool_name": "run_command",
                "tool_info": {"name": "run_command", "parameters": {"CommandLine": cmd}, "output": out}}}

        route_cmd = ".venv-kicad-ipc/bin/python scripts/kicad_route.py SP1.1 J1.1 --pid 21"
        lay_cmd = ".venv-kicad-ipc/bin/python scripts/kicad_lay.py --pid 21 batch --file batch.json"
        events = [
            cmd_event(route_cmd, '{"ok": false, "status": "pad_entry"}', code=1, state="ERROR"),
            cmd_event(lay_cmd, '{"ok": true, "status": "laid"}', code=0),
            cmd_event(".venv-kicad-ipc/bin/python scripts/kicad_edit.py --pid 21 list --net /MOTOR_A", '{"ok": true}'),
            cmd_event(".venv-kicad-ipc/bin/python scripts/mac.py look '#21'",
                      "/tmp/look.png\n1200 x 800 px of window #21 (PCB Editor: motor [Read Only] \u2014 PCB Editor); 1 px = 1.000 points."),
            cmd_event("python3 -c 'import kipy'", "ok"),
            *self.ok_events(),
        ]
        with self.fake_popen(events, operator=True):
            ok, _ = routing.run("/opt/bin/agy", "Route", self.model, None, self.root, self.env,
                                self.log, 60, pid=21, images=[self.img_a])
        self.assertTrue(ok)
        counts = routing.calls(self.log, board_stem="motor")
        self.assertEqual((counts["route"], counts["route_refused"], counts["lay"], counts["lay_laid"]), (1, 1, 1, 1))
        self.assertEqual((counts["ipc_copper_calls"], counts["gui_copper_calls"]), (1, 1))
        self.assertEqual(counts["edit_by_verb"], {"list": 1})
        self.assertEqual(counts["look"], 1)
        self.assertTrue(counts["looked_after_edit"])
        self.assertEqual(counts["wrapper_calls"], 1)

    def test_security_and_failure_paths_fail_closed(self):
        # Model reroute
        with self.fake_popen(self.ok_events(model="claude-opus-5-5-high"), operator=False):
            ok, detail = routing.run("/opt/bin/agy", "Review", self.model, None, self.root, self.env, self.log, 10, images=[self.img_a])
        self.assertFalse(ok)
        self.assertIn("changed the requested model", detail)
        # Forbidden tool in review mode
        bad_tool = {"event": "step_update", "step_update": {"step_type": "tool", "state": "DONE", "tool_name": "run_command",
                                                            "tool_info": {"name": "run_command", "parameters": {"CommandLine": "ls"}}}}
        with self.fake_popen([bad_tool, *self.ok_events()], operator=False):
            ok, detail = routing.run("/opt/bin/agy", "Review", self.model, None, self.root, self.env, self.log, 10, images=[self.img_a])
        self.assertFalse(ok)
        self.assertIn("Unexpected tool", detail)
        # Unauthorized file read in review mode
        bad_read = {"event": "step_update", "step_update": {"step_type": "tool", "state": "DONE", "tool_name": "read_file",
                                                            "tool_info": {"name": "read_file", "parameters": {"Path": str(self.board)}}}}
        with self.fake_popen([bad_read, *self.ok_events()], operator=False):
            ok, detail = routing.run("/opt/bin/agy", "Review", self.model, None, self.root, self.env, self.log, 10, images=[self.img_a])
        self.assertFalse(ok)
        self.assertIn("unauthorized file", detail)
        # Explicit read_paths (e.g. saved PCB and rules) are allowed alongside PNGs in review mode
        board_read = {"event": "step_update", "step_update": {"step_type": "tool", "state": "DONE", "tool_name": "read_file",
                                                              "tool_info": {"name": "read_file", "parameters": {"Path": str(self.board)}}}}
        with self.fake_popen([board_read, *self.ok_events()], operator=False):
            ok, detail = routing.run("/opt/bin/agy", "Review", self.model, None, self.root, self.env, self.log, 10,
                                     images=[self.img_a], read_paths=[self.board])
        self.assertTrue(ok, detail)
        self.assertIn(f"read_file({self.board.resolve()})", self.last_settings["permissions"]["allow"])
        other_file = self.root / "other.txt"
        other_file.write_text("x", encoding="utf-8")
        other_read = {"event": "step_update", "step_update": {"step_type": "tool", "state": "DONE", "tool_name": "read_file",
                                                              "tool_info": {"name": "read_file", "parameters": {"Path": str(other_file)}}}}
        with self.fake_popen([other_read, *self.ok_events()], operator=False):
            ok, detail = routing.run("/opt/bin/agy", "Review", self.model, None, self.root, self.env, self.log, 10,
                                     images=[self.img_a], read_paths=[self.board])
        self.assertFalse(ok)
        self.assertIn("unauthorized file", detail)
        # Denied actions reported in result
        denied_result = self.ok_events()
        denied_result[-1]["result"]["denied_actions"] = [{"action": "run_command"}]
        with self.fake_popen(denied_result, operator=False):
            ok, detail = routing.run("/opt/bin/agy", "Review", self.model, None, self.root, self.env, self.log, 10, images=[self.img_a])
        self.assertFalse(ok)
        self.assertIn("denied action", detail)
        # Error result
        err_result = [{"event": "result", "result": {"status": "ERROR", "error": "auth failed",
                                                     "usage": {"input_tokens": 0, "output_tokens": 0}}}]
        with self.fake_popen(err_result, exitcode=1, operator=False):
            ok, detail = routing.run("/opt/bin/agy", "Review", self.model, None, self.root, self.env, self.log, 10, images=[self.img_a])
        self.assertFalse(ok)
        self.assertIn("auth failed", detail)
        self.assertFalse(routing.tokens(self.log, self.model)["complete"])

    def test_timeout_kills_process_group(self):
        with patch.object(subprocess, "Popen") as popen, patch.object(routing.os, "killpg", create=True) as stop, \
                patch.object(routing.signal, "SIGKILL", 9, create=True):
            child = popen.return_value
            child.wait.side_effect = [subprocess.TimeoutExpired("agy", 10), 0]
            child.returncode = 1
            ok, detail = routing.run("/opt/bin/agy", "Route", self.model, None, self.root, self.env,
                                     self.log, 10, pid=99, images=[self.img_a])
        self.assertFalse(ok)
        self.assertIn("handoff grace", detail)
        stop.assert_called_once()

    def test_streams_events_and_invokes_live_status_callback(self):
        active_cmd = {
            "event": "step_update",
            "step_update": {
                "step_index": 3, "state": "ACTIVE", "step_type": "tool", "tool_name": "run_command",
                "tool_info": {"name": "run_command", "parameters": {"CommandLine": ".venv-kicad-ipc/bin/python scripts/kicad_lay.py batch --json '{}'"}},
            },
        }
        seen = []
        with self.fake_popen([active_cmd, *self.ok_events()], operator=True):
            ok, answer = routing.run(
                "/opt/bin/agy", "Route", self.model, None, self.root, self.env,
                self.log, 60, pid=21, images=[self.img_a], on_status=seen.append,
            )
        self.assertEqual((ok, answer), (True, "NOTHING."))
        self.assertEqual(seen, ["kicad_lay.py"])

    def test_review_recovers_when_disallowed_attempt_is_blocked_with_zero_output(self):
        # Blocked run_command (ACTIVE -> ERROR TOOL_ERROR Permission denied, empty output) recovers cleanly.
        blocked_cmd = [
            {"event": "step_update", "step_update": {
                "step_index": 5, "state": "ACTIVE", "step_type": "tool", "tool_name": "run_command",
                "tool_info": {"name": "run_command", "parameters": {"CommandLine": "python3 -c 'print(1)'"}}}},
            {"event": "step_update", "step_update": {
                "step_index": 5, "state": "ERROR", "step_type": "tool", "tool_name": "run_command",
                "tool_info": {"name": "run_command", "parameters": {"CommandLine": "python3 -c 'print(1)'"},
                              "error": {"type": "TOOL_ERROR", "message": "Permission denied. Matches user-configured deny rule."}}}},
        ]
        with self.fake_popen([*blocked_cmd, *self.ok_events()], operator=False):
            ok, answer = routing.run("/opt/bin/agy", "Review", self.model, None, self.root, self.env, self.log, 10, images=[self.img_a])
        self.assertEqual((ok, answer), (True, "NOTHING."))

        # Same with a matching result.denied_actions entry
        events_with_denied = [*blocked_cmd, *self.ok_events()]
        events_with_denied[-1]["result"]["denied_actions"] = [{"action": "run_command"}]
        with self.fake_popen(events_with_denied, operator=False):
            ok, answer = routing.run("/opt/bin/agy", "Review", self.model, None, self.root, self.env, self.log, 10, images=[self.img_a])
        self.assertEqual((ok, answer), (True, "NOTHING."))

        # Blocked unauthorized read_file (AGENTS.md -> ERROR TOOL_ERROR file does not exist, empty output) recovers cleanly.
        agents = self.root / "AGENTS.md"
        blocked_read = [
            {"event": "step_update", "step_update": {
                "step_index": 6, "state": "ACTIVE", "step_type": "tool", "tool_name": "read_file",
                "tool_info": {"name": "read_file", "parameters": {"Path": str(agents)}}}},
            {"event": "step_update", "step_update": {
                "step_index": 6, "state": "ERROR", "step_type": "tool", "tool_name": "read_file",
                "tool_info": {"name": "read_file", "parameters": {"Path": str(agents)},
                              "error": {"type": "TOOL_ERROR", "message": "file does not exist"}}}},
        ]
        with self.fake_popen([*blocked_read, *self.ok_events()], operator=False):
            ok, answer = routing.run("/opt/bin/agy", "Review", self.model, None, self.root, self.env, self.log, 10, images=[self.img_a])
        self.assertEqual((ok, answer), (True, "NOTHING."))

    def test_review_and_operator_fail_closed_on_executed_or_unclosed_violations(self):
        # (a) run_command ERROR that returned real command output fails closed
        executed_err = [
            {"event": "step_update", "step_update": {
                "step_index": 2, "state": "ACTIVE", "step_type": "tool", "tool_name": "run_command",
                "tool_info": {"name": "run_command", "parameters": {"CommandLine": "ls"}}}},
            {"event": "step_update", "step_update": {
                "step_index": 2, "state": "ERROR", "step_type": "tool", "tool_name": "run_command",
                "tool_info": {"name": "run_command", "parameters": {"CommandLine": "ls"},
                              "output": "Command exited with code 1.",
                              "error": {"type": "TOOL_ERROR", "message": "Permission denied"}}}},
        ]
        with self.fake_popen([*executed_err, *self.ok_events()], operator=False):
            ok, detail = routing.run("/opt/bin/agy", "Review", self.model, None, self.root, self.env, self.log, 10, images=[self.img_a])
        self.assertFalse(ok)
        self.assertIn("Unexpected tool", detail)

        # (b) disallowed tool or unauthorized read reaching DONE fails closed
        agents = self.root / "AGENTS.md"
        done_unauth = {"event": "step_update", "step_update": {
            "step_index": 3, "state": "DONE", "step_type": "tool", "tool_name": "read_file",
            "tool_info": {"name": "read_file", "parameters": {"Path": str(agents)}}}}
        with self.fake_popen([done_unauth, *self.ok_events()], operator=False):
            ok, detail = routing.run("/opt/bin/agy", "Review", self.model, None, self.root, self.env, self.log, 10, images=[self.img_a])
        self.assertFalse(ok)
        self.assertIn("unauthorized file", detail)

        # (c) ACTIVE disallowed call with no terminal ERROR fails closed after stream drains
        orphaned_active = {"event": "step_update", "step_update": {
            "step_index": 4, "state": "ACTIVE", "step_type": "tool", "tool_name": "run_command",
            "tool_info": {"name": "run_command", "parameters": {"CommandLine": "ls"}}}}
        with self.fake_popen([orphaned_active, *self.ok_events()], operator=False):
            ok, detail = routing.run("/opt/bin/agy", "Review", self.model, None, self.root, self.env, self.log, 10, images=[self.img_a])
        self.assertFalse(ok)
        self.assertIn("Unexpected tool", detail)

        # (d) Operator hitting forbidden tool or denied_actions still fails closed even on ERROR
        op_blocked_write = {"event": "step_update", "step_update": {
            "step_index": 5, "state": "ERROR", "step_type": "tool", "tool_name": "write_file",
            "tool_info": {"name": "write_file", "parameters": {"Path": str(self.board)},
                          "error": {"type": "TOOL_ERROR", "message": "Permission denied"}}}}
        with self.fake_popen([op_blocked_write, *self.ok_events()], operator=True):
            ok, detail = routing.run("/opt/bin/agy", "Route", self.model, None, self.root, self.env, self.log, 10, pid=21, images=[self.img_a])
        self.assertFalse(ok)
        self.assertIn("Unexpected tool", detail)

        op_denied = self.ok_events()
        op_denied[-1]["result"]["denied_actions"] = [{"action": "write_file"}]
        with self.fake_popen(op_denied, operator=True):
            ok, detail = routing.run("/opt/bin/agy", "Route", self.model, None, self.root, self.env, self.log, 10, pid=21, images=[self.img_a])
        self.assertFalse(ok)
        self.assertIn("denied action", detail)

    def test_calls_passes_duration_and_session_timing_extracts_breakdown(self):
        def cmd_event(step_idx, cmd, body, dur_s, code=0, state="DONE"):
            out = f"Created At: t\nCompleted At: t\nCommand exited with code {code}.\n\nCommand output:\n{body}"
            return {"event": "step_update", "step_update": {
                "step_index": step_idx, "state": state, "step_type": "tool", "tool_name": "run_command",
                "duration_seconds": dur_s,
                "tool_info": {"name": "run_command", "parameters": {"CommandLine": cmd}, "output": out}}}

        events = [
            {"event": "init", "conversation_id": "c1", "init": {"model": self.model, "cwd": str(self.root)}},
            cmd_event(1, ".venv-kicad-ipc/bin/python scripts/kicad_lay.py --pid 21 batch --json '{}'",
                      '{"ok": false, "status": "bad_request", "reason": "bad coordinates"}', 3.5, code=1, state="ERROR"),
            cmd_event(2, ".venv-kicad-ipc/bin/python scripts/kicad_lay.py --help", "usage: kicad_lay.py", 0.5),
            cmd_event(3, ".venv-kicad-ipc/bin/python scripts/kicad_lay.py --pid 21 batch --stdin",
                      '{"ok": true, "status": "laid"}', 12.0),
            {"event": "step_update", "step_update": {
                "step_index": 4, "state": "DONE", "step_type": "tool", "tool_name": "read_file", "duration_seconds": 1.0,
                "tool_info": {"name": "read_file", "parameters": {"Path": str(self.img_a)}}}},
            {"event": "result", "result": {"conversation_id": "c1", "status": "SUCCESS", "response": "done",
                                           "num_turns": 3, "duration_seconds": 100.0,
                                           "usage": {"input_tokens": 1000, "output_tokens": 500,
                                                     "thinking_tokens": 450, "cache_read_tokens": 200}}},
        ]
        with self.fake_popen(events, operator=True):
            ok, _ = routing.run("/opt/bin/agy", "Route", self.model, None, self.root, self.env,
                                self.log, 60, pid=21, images=[self.img_a])
        self.assertTrue(ok)
        counts = routing.calls(self.log, board_stem="motor")
        self.assertEqual(counts["help_calls"], 1)
        self.assertEqual(counts["tool_seconds"], 16.0)
        self.assertEqual(counts["refusal_seconds"], 3.5)
        self.assertEqual(counts["refusal_reasons"], {"kicad_lay: bad_request: bad coordinates": {"count": 1, "seconds": 3.5}})
        timing = routing.session_timing(self.log)
        self.assertEqual(timing, {"wall_s": 100.0, "tool_s": 17.0, "llm_s": 83.0, "thinking_tokens": 450})

    def test_session_timing_falls_back_to_step_updates_when_result_is_missing(self):
        # Timed-out single invocation with no result event + duplicate step_index update must deduplicate and sum cleanly.
        truncated = [
            {"event": "routing.invocation_started", "requested_model": self.model, "role": "operator"},
            {"event": "init", "conversation_id": "c1", "init": {"model": self.model, "cwd": str(self.root)}},
            {"event": "step_update", "step_update": {
                "step_index": 1, "state": "DONE", "step_type": "agent_response", "duration_seconds": 12.0,
                "usage": {"input_tokens": 100, "output_tokens": 900, "thinking_tokens": 800},
            }},
            # Duplicate step_index update must overwrite rather than double-count.
            {"event": "step_update", "step_update": {
                "step_index": 1, "state": "DONE", "step_type": "agent_response", "duration_seconds": 12.0,
                "usage": {"input_tokens": 100, "output_tokens": 900, "thinking_tokens": 800},
            }},
            {"event": "step_update", "step_update": {
                "step_index": 2, "state": "DONE", "step_type": "tool", "tool_name": "run_command", "duration_seconds": 5.5,
                "tool_info": {"name": "run_command", "parameters": {"CommandLine": "ls"}, "output": ""},
            }},
            {"event": "step_update", "step_update": {
                "step_index": 3, "state": "DONE", "step_type": "agent_response", "duration_seconds": 25.0,
                "usage": {"input_tokens": 200, "output_tokens": 2500, "thinking_tokens": 2100},
            }},
        ]
        log_a = self.root / "truncated.jsonl"
        log_a.write_text("\n".join(json.dumps(e) for e in truncated) + "\n", encoding="utf-8")
        self.assertEqual(
            routing.session_timing(log_a),
            {"wall_s": 42.5, "tool_s": 5.5, "llm_s": 37.0, "thinking_tokens": 2900},
        )

        # Multi-invocation: Round 1 has terminal result (no double-count against step_update), Round 2 times out mid-step.
        multi = [
            {"event": "routing.invocation_started", "requested_model": self.model, "role": "operator"},
            {"event": "step_update", "step_update": {
                "step_index": 1, "state": "DONE", "step_type": "agent_response", "duration_seconds": 10.0,
                "usage": {"input_tokens": 50, "output_tokens": 500, "thinking_tokens": 400},
            }},
            {"event": "step_update", "step_update": {
                "step_index": 2, "state": "DONE", "step_type": "tool", "tool_name": "run_command", "duration_seconds": 3.0,
                "tool_info": {"name": "run_command", "parameters": {"CommandLine": "ls"}, "output": ""},
            }},
            {"event": "result", "result": {"status": "SUCCESS", "duration_seconds": 20.0,
                                           "usage": {"input_tokens": 50, "output_tokens": 500, "thinking_tokens": 400}}},
            {"event": "routing.invocation_finished", "metered": True},
            *truncated,
        ]
        log_b = self.root / "multi.jsonl"
        log_b.write_text("\n".join(json.dumps(e) for e in multi) + "\n", encoding="utf-8")
        self.assertEqual(
            routing.session_timing(log_b),
            {"wall_s": 62.5, "tool_s": 8.5, "llm_s": 54.0, "thinking_tokens": 3300},
        )


if __name__ == "__main__":
    unittest.main()
