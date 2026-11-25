import json
import pathlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import routing_codex as routing


class CodexRoutingTests(unittest.TestCase):
    def test_unresponsive_operator_stops_after_handoff_grace(self):
        with patch.object(subprocess, "Popen") as popen, patch.object(routing.os, "killpg", create=True) as stop, \
                patch.object(routing.signal, "SIGKILL", 9, create=True):
            child = popen.return_value
            child.communicate.side_effect = subprocess.TimeoutExpired("operator", 60)
            ok, detail = routing.run("codex", "finish the gesture", self.model, "low", self.root, self.env,
                                     self.log, 60, pid=123)
            self.assertFalse(ok)
            self.assertIn("handoff grace", detail)
            stop.assert_called_once()
            child.wait.assert_called_once()

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name)
        self.log = self.root / "events.jsonl"
        self.model = "gpt-6.1-terra"
        self.env = {"CODEX_HOME": str(self.root), "BOARD": str(self.root / "motor.kicad_pcb"),
                    "SOCKET": "ipc:///tmp/kicad/api-123.sock", "ROUTING_HANDOFF_AT": "12345"}
        self.catalog = {"models": [{"slug": self.model, "shell_type": "unified_exec", "apply_patch_tool_type": "freeform",
                                    "input_modalities": ["text", "image"], "multi_agent_version": "v2",
                                    "experimental_supported_tools": ["arbitrary_tool"]}]}
        (self.root / "models_cache.json").write_text(json.dumps(self.catalog))

    def test_cli_catalog_keeps_shell_for_operator_and_removes_it_for_review(self):
        operator_file = self.root / "operator.json"
        review_file = self.root / "review.json"
        routing._catalog(self.env, self.model, operator_file, operator=True)
        routing._catalog(self.env, self.model, review_file, operator=False)
        op_entry, = json.loads(operator_file.read_text())["models"]
        rev_entry, = json.loads(review_file.read_text())["models"]
        self.assertEqual(op_entry["slug"], self.model)
        self.assertEqual(op_entry["shell_type"], "unified_exec")
        self.assertIsNone(op_entry["apply_patch_tool_type"])
        self.assertIsNone(op_entry["multi_agent_version"])
        self.assertEqual(op_entry["experimental_supported_tools"], [])
        self.assertEqual(op_entry["input_modalities"], ["text", "image"])
        self.assertEqual(rev_entry["shell_type"], "disabled")
        self.assertIsNone(rev_entry["apply_patch_tool_type"])
        with self.assertRaisesRegex(ValueError, "no usable entry"):
            routing._catalog(self.env, "gpt-missing", review_file)

    def test_operator_gets_shell_without_mcp_while_reviewer_stays_read_only(self):
        cmd = lambda pid: routing._argv("codex", self.model, "low", self.root, self.env["BOARD"], pid, self.root / "models.json", [])
        operator, reviewer = cmd(123), cmd(None)
        for argv in [operator, reviewer]:
            self.assertIn("--ignore-user-config", argv)
            self.assertIn("--ephemeral", argv)
            self.assertIn("--ignore-rules", argv)
            self.assertIn("never", argv)
            self.assertIn("mcp_servers={}", argv)
            self.assertIn("model_provider=\"openai\"", argv)
            self.assertIn("multi_agent", argv)
        self.assertIn("danger-full-access", operator)
        self.assertNotIn("shell_tool", operator)
        self.assertNotIn("unified_exec", operator)
        self.assertNotIn("view_image", operator)
        self.assertIn("read-only", reviewer)
        self.assertIn("shell_tool", reviewer)
        self.assertIn("unified_exec", reviewer)
        self.assertIn("view_image", reviewer)

    def fake(self, events, exitcode=0, operator=True):
        expected_cwd = self.root if operator else None

        class Child:
            def __init__(child, argv, **kwargs):
                if operator:
                    self.assertEqual(kwargs["cwd"], expected_cwd)
                    self.assertEqual(kwargs["env"]["SOCKET"], "ipc:///tmp/kicad/api-123.sock")
                    self.assertEqual(kwargs["env"]["ROUTING_HANDOFF_AT"], "12345")
                else:
                    self.assertEqual(kwargs["cwd"].parent, pathlib.Path(tempfile.gettempdir()))
                    self.assertNotIn("SOCKET", kwargs["env"])
                for event in events:
                    kwargs["stdout"].write((json.dumps(event) + "\n").encode())
                child.returncode = exitcode

            def communicate(child, data, timeout):
                self.assertEqual(data, b"inspect the board")

        return patch.object(subprocess, "Popen", Child)

    def invoke(self, events, exitcode=0, operator=True):
        with self.fake(events, exitcode, operator=operator):
            return routing.run("codex", "inspect the board", self.model, "low", self.root, self.env,
                               self.log, 10, pid=21 if operator else None)

    def messages(self):
        return [{"type": "item.completed", "item": {"type": "agent_message", "text": "NOTHING"}},
                {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 72, "output_tokens": 11}}]

    def test_metering_and_shell_calls_over_fresh_invocations(self):
        shell = lambda command, output="", code=0: {"type": "item.completed", "item": {
            "type": "command_execution", "command": command, "aggregated_output": output,
            "exit_code": code, "status": "completed" if code == 0 else "failed"}}
        route_cmd = "/bin/zsh -lc '.venv-kicad-ipc/bin/python scripts/kicad_route.py SP1.1 J1.1 --width 0.5 --pid 21'"
        refused = shell(route_cmd, '{"ok": false, "status": "pad_entry"}', code=1)
        closed = shell(route_cmd, '{"ok": true, "closed": true, "status": "closed"}', code=0)
        read_script = shell("sed -n '1,20p' scripts/kicad_route.py", "#!/usr/bin/env python3")
        edit = shell(".venv-kicad-ipc/bin/python scripts/kicad_edit.py --pid 21 list --net /MOTOR_A", '{"ok": true}')
        look = shell(".venv-kicad-ipc/bin/python scripts/mac.py look", "/tmp/look.png")
        idle = shell(".venv-kicad-ipc/bin/python scripts/kicad_ipc_status.py", "idle")
        other = shell("date", "Wed Nov 18 17:40:00 MST 2026")
        self.assertEqual(self.invoke([refused, closed, read_script, edit, look, idle, other, *self.messages()]), (True, "NOTHING"))
        self.assertEqual(self.invoke(self.messages()), (True, "NOTHING"))
        usage = routing.tokens(self.log, self.model)
        self.assertTrue(usage["complete"])
        self.assertEqual(len(usage["rounds"]), 2)
        self.assertEqual(usage["by_model"][self.model],
                         {"input": 56, "cache_read": 144, "output": 22, "thinking": None, "cache_write": None})
        counts = routing.calls(self.log)
        self.assertEqual((counts["route"], counts["route_closed"], counts["route_refused"]), (2, 1, 1))
        self.assertEqual(counts["route_by_status"], {"closed": 1, "pad_entry": 1})
        self.assertEqual((counts["edit"], counts["edit_by_verb"]), (1, {"list": 1}))
        self.assertEqual((counts["look"], counts["mac"], counts["other"]), (1, 1, 2))
        self.assertTrue(counts["looked_after_edit"])
        self.assertEqual(counts["repeats"], {route_cmd: 2})

    def test_look_before_route_does_not_count_as_looked_after_edit(self):
        shell = lambda command, output="", code=0: {"type": "item.completed", "item": {
            "type": "command_execution", "command": command, "aggregated_output": output,
            "exit_code": code, "status": "completed"}}
        look = shell(".venv-kicad-ipc/bin/python scripts/mac.py look", "/tmp/look.png")
        closed = shell(".venv-kicad-ipc/bin/python scripts/kicad_route.py SP1.1 J1.1 --pid 21",
                       '{"ok": true, "closed": true, "status": "closed"}')
        self.assertEqual(self.invoke([look, closed, *self.messages()]), (True, "NOTHING"))
        counts = routing.calls(self.log)
        self.assertEqual(counts["look"], 1)
        self.assertFalse(counts["looked_after_edit"])

    def test_compound_shell_command_counts_look_and_edit_in_order(self):
        shell = lambda command, output="", code=0: {"type": "item.completed", "item": {
            "type": "command_execution", "command": command, "aggregated_output": output,
            "exit_code": code, "status": "completed"}}
        closed = shell(".venv-kicad-ipc/bin/python scripts/kicad_route.py SP1.1 J1.1 --pid 21",
                       '{"ok": true, "closed": true, "status": "closed"}')
        chained = shell(
            "/bin/zsh -lc \".venv-kicad-ipc/bin/python scripts/mac.py look 'PCB Editor'; "
            ".venv-kicad-ipc/bin/python scripts/kicad_edit.py --pid 21 list --net /MOTOR_A; "
            ".venv-kicad-ipc/bin/python scripts/kicad_ipc_status.py\"",
            "/tmp/look.png",
        )
        self.assertEqual(self.invoke([closed, chained, *self.messages()]), (True, "NOTHING"))
        counts = routing.calls(self.log)
        self.assertEqual((counts["route_closed"], counts["look"], counts["edit"], counts["mac"]), (1, 1, 1, 1))
        self.assertTrue(counts["looked_after_edit"])

    def test_failed_or_non_editor_look_does_not_count_as_visual_inspection(self):
        shell = lambda command, output="", code=0: {"type": "item.completed", "item": {
            "type": "command_execution", "command": command, "aggregated_output": output,
            "exit_code": code, "status": "failed" if code else "completed"}}
        closed = shell(".venv-kicad-ipc/bin/python scripts/kicad_route.py SP1.1 J1.1 --pid 21",
                       '{"ok": true, "closed": true, "status": "closed"}')
        cases = [
            shell(".venv-kicad-ipc/bin/python scripts/mac.py look 'PCB Editor'", "MacError: screen locked", code=1),
            shell(".venv-kicad-ipc/bin/python scripts/mac.py look 'PCB Editor'", "", code=0),
            shell(".venv-kicad-ipc/bin/python scripts/mac.py look Finder", "/tmp/look.png\n100 x 100 px of window #9 (Finder: Desktop)", code=0),
            shell(".venv-kicad-ipc/bin/python scripts/mac.py look '#9'", "/tmp/look.png\n100 x 100 px of window #9 (Finder: Desktop)", code=0),
        ]
        for bad_look in cases:
            with self.subTest(bad_look=bad_look["item"]["command"]):
                self.log.unlink(missing_ok=True)
                self.assertEqual(self.invoke([closed, bad_look, *self.messages()]), (True, "NOTHING"))
                counts = routing.calls(self.log)
                self.assertEqual(counts["look"], 0)
                self.assertFalse(counts["looked_after_edit"])

    def test_look_checks_assigned_board_title_and_rejects_other_projects_pcb(self):
        shell = lambda command, output="", code=0: {"type": "item.completed", "item": {
            "type": "command_execution", "command": command, "aggregated_output": output,
            "exit_code": code, "status": "completed"}}
        closed = shell(".venv-kicad-ipc/bin/python scripts/kicad_route.py SP1.1 J1.1 --pid 21",
                       '{"ok": true, "closed": true, "status": "closed"}')
        other_board = shell(
            ".venv-kicad-ipc/bin/python scripts/mac.py look '#99'",
            "/tmp/look.png\n1200 x 800 px of window #99 (PCB Editor: other_board \u2014 PCB Editor); 1 px = 1.000 points.",
        )
        right_board = shell(
            ".venv-kicad-ipc/bin/python scripts/mac.py look '#21'",
            "/tmp/look.png\n1200 x 800 px of window #21 (PCB Editor: *motor_board_v1 [Read Only] \u2014 PCB Editor); 1 px = 1.000 points.",
        )
        self.assertEqual(self.invoke([closed, other_board, *self.messages()]), (True, "NOTHING"))
        self.assertFalse(routing.calls(self.log, board_stem="motor_board_v1")["looked_after_edit"])
        self.log.unlink(missing_ok=True)
        self.assertEqual(self.invoke([closed, right_board, *self.messages()]), (True, "NOTHING"))
        self.assertTrue(routing.calls(self.log, board_stem="motor_board_v1")["looked_after_edit"])

    def test_ad_hoc_python_wrapper_around_kicad_route_or_kipy_is_flagged(self):
        shell = lambda command, output="", code=0: {"type": "item.completed", "item": {
            "type": "command_execution", "command": command, "aggregated_output": output,
            "exit_code": code, "status": "completed"}}
        for cmd in (
            "/bin/zsh -lc \"PYTHONPATH=scripts python - <<'PY'\nimport kicad_route as r\nr.main()\nPY\"",
            "/bin/zsh -lc 'PYTHONPATH=scripts /Users/builder/demo-board/.venv-kicad-ipc/bin/python /tmp/motorwidthroute.py'",
            "/bin/zsh -lc 'python3 -c \"from kipy import KiCad\"'",
            "/bin/zsh -lc 'python3 -c \"import kicad_lay\"'",
        ):
            with self.subTest(cmd=cmd):
                self.log.unlink(missing_ok=True)
                self.assertEqual(self.invoke([shell(cmd, "ok"), *self.messages()]), (True, "NOTHING"))
                self.assertGreater(routing.calls(self.log)["wrapper_calls"], 0)

    def test_kicad_lay_calls_and_copper_mode_counters(self):
        shell = lambda command, output="", code=0: {"type": "item.completed", "item": {
            "type": "command_execution", "command": command, "aggregated_output": output,
            "exit_code": code, "status": "failed" if code else "completed"}}
        events = [
            shell(".venv-kicad-ipc/bin/python scripts/kicad_lay.py --pid 21 pads J1.1 J2.1", '{"ok": true, "status": "done"}'),
            shell(".venv-kicad-ipc/bin/python scripts/kicad_lay.py --pid 21 route J1.1 J2.1", '{"ok": false, "status": "drc_failed"}', code=1),
            shell(".venv-kicad-ipc/bin/python scripts/kicad_edit.py --pid 21 delete abc && .venv-kicad-ipc/bin/python scripts/kicad_lay.py --pid 21 batch --stdin", '{"ok": true, "verb": "delete", "status": "done"}\n{"ok": true, "status": "laid"}'),
            shell(".venv-kicad-ipc/bin/python scripts/kicad_lay.py batch --help; .venv-kicad-ipc/bin/python scripts/kicad_lay.py route --help", "usage: kicad_lay.py batch [-h]"),
            shell(".venv-kicad-ipc/bin/python scripts/mac.py look '#21'",
                  "/tmp/look.png\n1200 x 800 px of window #21 (PCB Editor: motor [Read Only] \u2014 PCB Editor); 1 px = 1.000 points."),
            *self.messages(),
        ]
        self.assertEqual(self.invoke(events), (True, "NOTHING"))
        counts = routing.calls(self.log, board_stem="motor")
        self.assertEqual((counts["lay"], counts["lay_laid"], counts["lay_refused"]), (5, 1, 1))
        self.assertEqual((counts["ipc_copper_calls"], counts["gui_copper_calls"]), (2, 0))
        self.assertTrue(counts["looked_after_edit"])

    def test_failed_round_cannot_inherit_success_and_partial_usage_is_unknown(self):
        self.invoke(self.messages())
        success, detail = self.invoke([{"type": "turn.failed", "error": {"message": "no connection"}}], 1)
        self.assertFalse(success)
        self.assertIn("no connection", detail)
        self.assertFalse(routing.tokens(self.log, self.model)["complete"])

    def test_missing_or_unusable_usage_and_forbidden_tools_fail_closed(self):
        for events in [[*self.messages()[:-1]],
                       [self.messages()[0], {"type": "turn.completed", "usage": {"input_tokens": 1}}],
                       [{"type": "item.completed", "item": {"type": "file_change"}}, *self.messages()],
                       [{"type": "model.rerouted", "model": "gpt-another"}, *self.messages()],
                       [{"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "external", "tool": "some_tool"}},
                        *self.messages()]]:
            with self.subTest(events=events):
                self.assertFalse(self.invoke(events)[0])
        self.assertFalse(self.invoke([{"type": "item.completed", "item": {"type": "command_execution", "command": "ls"}},
                                      *self.messages()], operator=False)[0])

    def test_kicad_lay_batch_json_and_stdin_accounting(self):
        shell = lambda command, output="", code=0: {"type": "item.completed", "item": {
            "type": "command_execution", "command": command, "aggregated_output": output,
            "exit_code": code, "status": "completed"}}
        for cmd in (
            "/bin/zsh -lc \".venv-kicad-ipc/bin/python scripts/kicad_lay.py --pid 123 batch --stdin <<'EOF'\n{\\\"routes\\\":[]}\nEOF\"",
            ".venv-kicad-ipc/bin/python scripts/kicad_lay.py --pid 123 batch --json '{\"routes\":[]}'",
        ):
            with self.subTest(cmd=cmd):
                self.log.unlink(missing_ok=True)
                events = [shell(cmd, '{"ok": true, "status": "laid"}'), *self.messages()]
                self.assertEqual(self.invoke(events), (True, "NOTHING"))
                counts = routing.calls(self.log, board_stem="motor")
                self.assertEqual((counts["lay"], counts["ipc_copper_calls"], counts["wrapper_calls"]), (1, 1, 0))

    def test_cache_writes_and_thinking_do_not_inflate_the_graph_total(self):
        reading = {"input_tokens": 120, "cached_input_tokens": 61, "cache_write_input_tokens": 7,
                   "output_tokens": 13, "reasoning_output_tokens": 4}
        usage = routing._usage({"usage": reading})
        self.assertEqual(usage["input"], 52)
        self.assertEqual(sum(usage[k] for k in ("input", "cache_read", "cache_write", "output")), 133)
        self.assertEqual(usage["thinking"], 4)

    def test_kicad_lay_zones_pour_and_delete_zones_accounting(self):
        shell = lambda command, output="", code=0: {"type": "item.completed", "item": {
            "type": "command_execution", "command": command, "aggregated_output": output,
            "exit_code": code, "status": "completed"}}
        # zones alone is read-only (0 copper edits)
        events_ro = [shell(".venv-kicad-ipc/bin/python scripts/kicad_lay.py --pid 21 zones", '{"ok": true, "verb": "zones", "status": "ok"}'), *self.messages()]
        self.assertEqual(self.invoke(events_ro), (True, "NOTHING"))
        ro_counts = routing.calls(self.log, board_stem="motor")
        self.assertEqual((ro_counts["lay"], ro_counts["ipc_copper_calls"], ro_counts["lay_refused"]), (1, 0, 0))

        # pour + delete-zones + look counts both copper edits in lay_laid and ipc_copper_calls
        self.log.unlink(missing_ok=True)
        events_edit = [
            shell(".venv-kicad-ipc/bin/python scripts/kicad_lay.py --pid 21 zones", '{"ok": true, "verb": "zones", "status": "ok"}'),
            shell(".venv-kicad-ipc/bin/python scripts/kicad_lay.py --pid 21 pour --layer In1.Cu --net GND --board-outline", '{"ok": true, "verb": "pour", "status": "laid"}'),
            shell(".venv-kicad-ipc/bin/python scripts/kicad_lay.py --pid 21 delete-zones z-1", '{"ok": true, "verb": "delete-zones", "status": "deleted"}'),
            shell(".venv-kicad-ipc/bin/python scripts/mac.py look '#21'",
                  "/tmp/look.png\n1200 x 800 px of window #21 (PCB Editor: motor [Read Only] \u2014 PCB Editor); 1 px = 1.000 points."),
            *self.messages(),
        ]
        self.assertEqual(self.invoke(events_edit), (True, "NOTHING"))
        ed_counts = routing.calls(self.log, board_stem="motor")
        self.assertEqual((ed_counts["lay"], ed_counts["lay_laid"], ed_counts["lay_refused"], ed_counts["ipc_copper_calls"]), (3, 2, 0, 2))
        self.assertTrue(ed_counts["looked_after_edit"])

    def test_view_only_mac_commands_do_not_count_as_gui_copper_calls(self):
        shell = lambda command, output="", code=0: {"type": "item.completed", "item": {
            "type": "command_execution", "command": command, "aggregated_output": output,
            "exit_code": code, "status": "completed"}}
        # IPC pour/route followed by view-only Home, pageup/pagedown, Zoom to Fit, and look:
        view_events = [
            shell('.venv-kicad-ipc/bin/python scripts/kicad_lay.py --pid 21 batch --json \'{"routes":[]}\'', '{"ok": true, "status": "laid"}'),
            shell('.venv-kicad-ipc/bin/python scripts/mac.py keys "PCB Editor" Home', ""),
            shell('.venv-kicad-ipc/bin/python scripts/mac.py keys "PCB Editor" pageup pagedown', ""),
            shell('.venv-kicad-ipc/bin/python scripts/mac.py menu "PCB Editor" "View" "Zoom to Fit"', ""),
            shell('.venv-kicad-ipc/bin/python scripts/mac.py look "PCB Editor"',
                  '/tmp/look.png\n1600 x 800 px of window #21 (PCB Editor: motor \u2014 PCB Editor); 1 px = 1.000 points.'),
            # Another view-only call after look must NOT clear looked_after_edit
            shell('.venv-kicad-ipc/bin/python scripts/mac.py keys "PCB Editor" Home', ""),
            *self.messages(),
        ]
        self.assertEqual(self.invoke(view_events), (True, "NOTHING"))
        counts = routing.calls(self.log, board_stem="motor")
        self.assertEqual((counts["mac"], counts["gui_copper_calls"], counts["ipc_copper_calls"]), (4, 0, 1))
        self.assertTrue(counts["looked_after_edit"])

        # Editing hotkeys/menus/type/mouse STILL increment gui_copper_calls and clear looked_after_edit
        for gui_cmd in (
            '.venv-kicad-ipc/bin/python scripts/mac.py keys "PCB Editor" x',
            '.venv-kicad-ipc/bin/python scripts/mac.py keys "PCB Editor" v',
            '.venv-kicad-ipc/bin/python scripts/mac.py keys "PCB Editor" cmd+z',
            '.venv-kicad-ipc/bin/python scripts/mac.py menu "PCB Editor" "Edit" "Undo"',
            '.venv-kicad-ipc/bin/python scripts/mac.py type "PCB Editor" "0.5"',
            '.venv-kicad-ipc/bin/python scripts/mac.py mouse click 100 100',
        ):
            with self.subTest(gui_cmd=gui_cmd):
                self.log.unlink(missing_ok=True)
                ev = [
                    shell('.venv-kicad-ipc/bin/python scripts/mac.py look "PCB Editor"',
                          '/tmp/look.png\n1600 x 800 px of window #21 (PCB Editor: motor \u2014 PCB Editor); 1 px = 1.000 points.'),
                    shell(gui_cmd, ""),
                    *self.messages(),
                ]
                self.assertEqual(self.invoke(ev), (True, "NOTHING"))
                c = routing.calls(self.log, board_stem="motor")
                self.assertEqual(c["gui_copper_calls"], 1)
                self.assertFalse(c["looked_after_edit"])

    def test_audit_commands_duration_and_passive_refusal_reasons(self):
        # 4-tuple entries still work without durations; 5-tuple entries aggregate seconds, help calls and reasons.
        entries = [
            (".venv-kicad-ipc/bin/python scripts/kicad_lay.py --pid 21 batch --json '{}'",
             '{"ok": false, "status": "bad_request", "reason": "route \'to\' must be a REF.PAD string"}', 1, "failed", 4.5),
            (".venv-kicad-ipc/bin/python scripts/kicad_lay.py --help", "usage: kicad_lay.py", 0, "completed", 1.25),
            (".venv-kicad-ipc/bin/python scripts/kicad_lay.py --pid 21 batch --json '{}'",
             '{"ok": true, "status": "laid"}', 0, "completed", 6.0),
            (".venv-kicad-ipc/bin/python scripts/kicad_route.py A.1 B.1 --pid 21",
             '{"ok": false, "status": "pad_entry", "reason": "crossed edge diagonally"}', 1, "failed"),
        ]
        audited = routing.audit_commands(entries, board_stem="motor")
        self.assertEqual(audited["help_calls"], 1)
        self.assertEqual(audited["tool_seconds"], 11.75)
        self.assertEqual(audited["refusal_seconds"], 4.5)
        self.assertEqual(
            audited["refusal_reasons"]["kicad_lay: bad_request: route 'to' must be a REF.PAD string"],
            {"count": 1, "seconds": 4.5},
        )
        self.assertEqual(
            audited["refusal_reasons"]["kicad_route: pad_entry: crossed edge diagonally"],
            {"count": 1, "seconds": 0.0},
        )


class V27TelemetryTests(unittest.TestCase):
    def test_tool_status_and_reason_preserves_directions_up_to_220_chars(self) -> None:
        payload = {
            "status": "route_quality",
            "reason": (
                "route quality rejected new error(s): acute_or_reversing_corner on /F_CLK "
                "at (138.8, 99.825) (directions [(1, 0), (-1, 0)])"
            ),
        }
        status, reason = routing._tool_status_and_reason(json.dumps(payload), "kicad_lay.py")
        self.assertEqual(status, "route_quality")
        self.assertGreater(len(reason), 120)
        self.assertLessEqual(len(reason), 220)
        self.assertIn("directions [(1, 0), (-1, 0)]", reason)

    def test_normalize_refusal_reason_groups_pad_net_coord_and_directions(self) -> None:
        a1 = "kicad_lay: bad_request: U2.6: neither short edge faces (143.137, 101.125)"
        a2 = "kicad_lay: bad_request: U2.5: neither short edge faces (143.137, 100.475)"
        self.assertEqual(routing.normalize_refusal_reason(a1), routing.normalize_refusal_reason(a2))

        b1 = (
            "kicad_lay: route_quality: route quality rejected new error(s): "
            "acute_or_reversing_corner on /F_CLK at (138.8, 99.825) (directions [(1, 0), (-1, 0)])"
        )
        b2 = (
            "kicad_lay: route_quality: route quality rejected new error(s): "
            "acute_or_reversing_corner on /F_LATCH at (137.4, 100.475) (directions [(0, 1), (0, -1)])"
        )
        self.assertEqual(routing.normalize_refusal_reason(b1), routing.normalize_refusal_reason(b2))

        c1 = "kicad_lay: no_target_copper: to_point [10.0, 20.0] on F.Cu (/NET_A) does not land on any existing /NET_A track or via"
        c2 = "kicad_lay: no_target_copper: to_point [15.5, 30.25] on F.Cu (/NET_B) does not land on any existing /NET_B track or via"
        self.assertEqual(routing.normalize_refusal_reason(c1), routing.normalize_refusal_reason(c2))

        summary = routing.summarize_refusals([
            {"help_calls": 0, "refusal_seconds": 10.0, "refusal_reasons": {a1: {"count": 2, "seconds": 6.0}, b1: {"count": 1, "seconds": 4.0}}},
            {"help_calls": 1, "refusal_seconds": 5.0, "refusal_reasons": {a2: {"count": 1, "seconds": 3.0}, b2: {"count": 1, "seconds": 2.0}}},
        ])
        self.assertIsNotNone(summary)
        self.assertEqual(len(summary["by_reason"]), 4)
        self.assertIn(a1, summary["by_reason"])
        self.assertIn(a2, summary["by_reason"])
        self.assertEqual(len(summary["repeated_across_steps"]), 2)
        self.assertTrue(all(item["steps"] == 2 for item in summary["repeated_across_steps"]))


class V28TelemetryTests(unittest.TestCase):
    def test_normalize_refusal_reason_groups_drc_net_pairs_clearances_and_obstacles(self) -> None:
        s1 = "kicad_lay: drc_failed: new DRC error: Items shorting two nets (nets /SGND and GND)"
        s2 = "kicad_lay: drc_failed: new DRC error: Items shorting two nets (nets +5V and /DATA_C)"
        s3 = "kicad_lay: drc_failed: new DRC error: Items shorting two nets (nets /DATA_C and Net-(U6-Q6))"
        s4 = "kicad_lay: drc_failed: new DRC error: Items shorting two nets (nets GND and /DATA_OUT_Q)"
        normalized_shorts = {routing.normalize_refusal_reason(s) for s in (s1, s2, s3, s4)}
        self.assertEqual(
            normalized_shorts,
            {"kicad_lay: drc_failed: new DRC error: Items shorting two nets (nets <net> and <net>)"},
        )

        c1 = "kicad_lay: drc_failed: new DRC error: Clearance violation (clearance 0.1500 mm; actual 0.1305 mm)"
        c2 = "kicad_lay: drc_failed: new DRC error: Clearance violation (clearance 0.1500 mm; actual 0.0820 mm)"
        self.assertEqual(routing.normalize_refusal_reason(c1), routing.normalize_refusal_reason(c2))
        self.assertIn("(clearance <mm>; actual <mm>)", routing.normalize_refusal_reason(c1))

        o1 = (
            "kicad_lay: obstacle_collision: planned /DATA_C (158.0, 101.9)->(161.0, 101.9) on F.Cu "
            "hits existing +5V via at (158.825, 101.9) d=0.6 mm (gap=-0.100 mm, need 0.150 mm)"
        )
        o2 = (
            "kicad_lay: obstacle_collision: planned Net-(U7-1~{RD}) (120.0, 88.4)->(125.0, 88.4) on F.Cu "
            "hits existing Net-(U6-Q6) via at (122.0, 88.4) d=0.5 mm (gap=-0.200 mm, need 0.200 mm)"
        )
        self.assertEqual(routing.normalize_refusal_reason(o1), routing.normalize_refusal_reason(o2))
        self.assertIn("planned <net> (<x,y>)->(<x,y>) on F.Cu hits existing <net> via at (<x,y>) d=<mm> (gap=<mm>, need <mm>)",
                      routing.normalize_refusal_reason(o1))

        summary = routing.summarize_refusals([
            {"help_calls": 0, "refusal_seconds": 12.0, "refusal_reasons": {s1: {"count": 1, "seconds": 12.0}}},
            {"help_calls": 0, "refusal_seconds": 36.0, "refusal_reasons": {
                s2: {"count": 1, "seconds": 12.0},
                s3: {"count": 1, "seconds": 12.0},
                s4: {"count": 1, "seconds": 12.0},
            }},
        ])
        self.assertEqual(len(summary["by_reason"]), 4)
        self.assertEqual(
            summary["repeated_across_steps"],
            [{
                "reason": "kicad_lay: drc_failed: new DRC error: Items shorting two nets (nets <net> and <net>)",
                "steps": 2,
                "count": 4,
                "seconds": 48.0,
            }],
        )

    def test_audit_commands_tracks_other_by_cmd_and_summarize_extra_tools(self) -> None:
        entries = [
            ("python3 scripts/kicad_edit.py --pid 1 list --vias", "[]", 0, "completed", 0.5),
            ("python3 scripts/kicad_edit.py delete --id seg-1", "{}", 0, "completed", 0.4),
            ("FOO=1 python3 -c \"print('hi')\"", "hi", 0, "completed", 0.2),
            ("/bin/zsh -lc 'grep -n GND board-context.txt | head -n 5'", "line", 0, "completed", 0.1),
        ]
        audited = routing.audit_commands(entries, board_stem="base")
        self.assertEqual(audited["edit_by_verb"], {"delete": 1, "list": 1})
        self.assertEqual(audited["other"], 2)
        self.assertEqual(audited["other_by_cmd"], {"grep": 1, "python3": 1})

        agg = routing.summarize_extra_tools([
            {"edit_by_verb": {"list": 3, "delete": 1}, "other_by_cmd": {"python3": 1}},
            {"edit_by_verb": {"list": 2}, "other_by_cmd": {"grep": 2, "python3": 1}},
        ])
        self.assertEqual(agg, {"edit_by_verb": {"delete": 1, "list": 5}, "other_by_cmd": {"grep": 2, "python3": 2}})
        self.assertIsNone(routing.summarize_extra_tools([{"edit_by_verb": {}, "other_by_cmd": {}}]))


class V281AuditAndVerbTests(unittest.TestCase):
    def test_edit_verb_ignores_truncation_ellipses_and_refused_lay_keeps_looked_after_edit(self) -> None:
        look_ok = (
            "python3 scripts/mac.py look \"PCB Editor\"",
            "/tmp/look.png\n1200 x 800 px of window #10 (PCB Editor: base \u2014 PCB Editor); 1 px = 1.000 points.",
            0,
            "completed",
            0.3,
        )
        entries = [
            ("python3 scripts/kicad_lay.py --pid 10 batch --json '{}'", json.dumps({"status": "laid", "verb": "batch"}), 0, "completed", 1.2),
            look_ok,
            ("python3 scripts/mac.py look \"PCB Editor\"; python3 scripts/kicad_edit.py \u2026", "/tmp/look.png", 0, "completed", 0.4),
            ("python3 scripts/kicad_edit.py --pid 10\u2026", "{}", 0, "completed", 0.2),
            ("python3 scripts/kicad_lay.py --pid 10 batch --stdin", json.dumps({"status": "obstacle_collision", "reason": "hits via"}), 1, "failed", 0.2),
            ("python3 scripts/kicad_lay.py --pid 10 \u2026", json.dumps({"status": "bad_request", "reason": "bad"}), 1, "failed", 0.1),
        ]
        audited = routing.audit_commands(entries, board_stem="base")
        self.assertTrue(audited["looked_after_edit"])
        self.assertEqual(audited["edit_by_verb"], {"none": 2})
        self.assertEqual(audited["lay_refused"], 2)
        self.assertEqual(audited["ipc_copper_calls"], 2)

        # Genuine delete/cut after look still resets looked_after_edit, even if args end in truncation ellipsis.
        entries_with_delete = entries + [
            ("python3 scripts/kicad_edit.py delete --id seg-1 --extra \u2026", "{}", 0, "completed", 0.2),
        ]
        audited_del = routing.audit_commands(entries_with_delete, board_stem="base")
        self.assertFalse(audited_del["looked_after_edit"])
        self.assertEqual(audited_del["edit_by_verb"].get("delete"), 1)

    def test_chained_lay_orders_and_short_circuits_track_each_subcommand(self) -> None:
        look_ok = (
            'python3 scripts/mac.py look "PCB Editor"',
            "/tmp/look.png\n1200 x 800 px of window #10 (PCB Editor: base \u2014 PCB Editor); 1 px = 1.000 points.",
            0,
            "completed",
            0.2,
        )
        # 1. Forward order (pour && zones) followed by look marks looked_after_edit=True.
        fwd = [
            (
                "python3 scripts/kicad_lay.py --pid 1 pour --net GND --layer In1.Cu && "
                "python3 scripts/kicad_lay.py --pid 1 zones",
                '{"verb": "pour", "status": "laid"}\n{"verb": "zones", "status": "ok", "count": 2}\n',
                0,
                "completed",
                0.5,
            ),
            look_ok,
        ]
        a_fwd = routing.audit_commands(fwd, board_stem="base")
        self.assertTrue(a_fwd["looked_after_edit"])
        self.assertEqual(a_fwd["ipc_copper_calls"], 1)
        self.assertEqual(a_fwd["lay_by_status"], {"laid": 1, "ok": 1})
        self.assertEqual(a_fwd["lay_refused"], 0)

        # 2. Reverse order (zones && pour) keeps both ok and laid distinct.
        rev = [
            (
                "python3 scripts/kicad_lay.py --pid 1 zones && "
                "python3 scripts/kicad_lay.py --pid 1 pour --net +3V3 --layer In2.Cu",
                '{"verb": "zones", "status": "ok"}\n{"verb": "pour", "status": "laid"}\n',
                0,
                "completed",
                0.4,
            ),
        ]
        a_rev = routing.audit_commands(rev, board_stem="base")
        self.assertFalse(a_rev["looked_after_edit"])
        self.assertEqual(a_rev["ipc_copper_calls"], 1)
        self.assertEqual(a_rev["lay_by_status"], {"laid": 1, "ok": 1})
        self.assertEqual(a_rev["lay_refused"], 0)

        # 3. Short-circuited failure (pour drc_failed && zones && route) counts 1 refusal and 0 phantom edits.
        short_fail = [
            (
                "python3 scripts/kicad_lay.py --pid 1 pour --net GND --layer In1.Cu && "
                "python3 scripts/kicad_lay.py --pid 1 zones && "
                "python3 scripts/kicad_lay.py --pid 1 route --net A --from U1.1 --to U2.1",
                '{"verb": "pour", "status": "drc_failed", "reason": "short circuit"}\n',
                1,
                "failed",
                0.3,
            ),
        ]
        a_short = routing.audit_commands(short_fail, board_stem="base")
        self.assertEqual(a_short["lay_refused"], 1)
        self.assertEqual(a_short["lay_laid"], 0)
        self.assertEqual(a_short["ipc_copper_calls"], 1)
        self.assertFalse(a_short["looked_after_edit"])
        self.assertEqual(len(a_short["refusal_reasons"]), 1)

        # 4. Chained mutating calls where first commits copper and second refuses (pour laid && route drc_failed).
        partial = [
            (
                "python3 scripts/kicad_lay.py --pid 1 pour --net GND --layer In1.Cu && "
                "python3 scripts/kicad_lay.py --pid 1 route --net A --from U1.1 --to U2.1",
                '{"verb": "pour", "status": "laid"}\n{"verb": "route", "status": "drc_failed", "reason": "blocked"}\n',
                1,
                "failed",
                0.4,
            ),
        ]
        a_partial = routing.audit_commands(partial, board_stem="base")
        self.assertEqual(a_partial["lay_laid"], 1)
        self.assertEqual(a_partial["lay_refused"], 1)
        self.assertEqual(a_partial["ipc_copper_calls"], 2)

        # 5. Subsequent mutating pour && pads after look resets looked_after_edit to False.
        after = fwd + [
            (
                "python3 scripts/kicad_lay.py --pid 1 pour --net +3V3 --layer In2.Cu && "
                "python3 scripts/kicad_lay.py --pid 1 pads --net +3V3",
                '{"verb": "pour", "status": "laid"}\n{"verb": "pads", "status": "ok"}\n',
                0,
                "completed",
                0.4,
            ),
        ]
        a_after = routing.audit_commands(after, board_stem="base")
        self.assertFalse(a_after["looked_after_edit"])
        self.assertEqual(a_after["ipc_copper_calls"], 2)


if __name__ == "__main__":
    unittest.main()
