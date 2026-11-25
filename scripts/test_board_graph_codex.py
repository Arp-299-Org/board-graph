"""Isolated repeat-checkpoint and process-tree tests; no model, board, Mac or network."""
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import board_graph_codex as codex
from no_window import KWARGS


def completed(command, code):
    return {"type": "item.completed", "item": {"type": "command_execution", "command": command,
                                                 "status": "completed" if code == 0 else "failed", "exit_code": code}}


class RepeatGuardTests(unittest.TestCase):
    def test_failed_command_survives_intervening_inspection_and_resets_on_input_change_or_success(self):
        current = ["one"]
        guard = codex.RepeatGuard(None, 3, fingerprint=lambda root: current[0])
        self.assertIsNone(guard.check(completed("fail", 1)))
        self.assertIsNone(guard.check(completed("inspect", 0)))
        self.assertIsNone(guard.check(completed("fail", 1)))
        current[0] = "two"
        self.assertIsNone(guard.check(completed("fail", 1)))
        self.assertIsNone(guard.check(completed("fail", 0)))
        self.assertIsNone(guard.check(completed("fail", 1)))
        self.assertIsNone(guard.check(completed("inspect", 0)))
        self.assertIsNone(guard.check(completed("fail", 1)))
        self.assertIn("3 failures of command sha256", guard.check(completed("fail", 1)))

    def test_change_and_revert_seen_between_failures_resets_the_count(self):
        current = ["one"]
        guard = codex.RepeatGuard(None, 3, fingerprint=lambda root: current[0])
        self.assertIsNone(guard.check(completed("fail", 1)))
        self.assertIsNone(guard.check(completed("fail", 1)))
        current[0] = "two"
        self.assertIsNone(guard.check(completed("edit", 0)))
        current[0] = "one"
        self.assertIsNone(guard.check(completed("revert", 0)))
        self.assertIsNone(guard.check(completed("fail", 1)))
        self.assertIsNone(guard.check(completed("fail", 1)))
        self.assertIn("3 failures", guard.check(completed("fail", 1)))

    def test_unknown_command_result_refuses_to_claim_checkpoint_coverage(self):
        guard = codex.RepeatGuard(None, 3, fingerprint=lambda root: "x")
        with self.assertRaisesRegex(ValueError, "exit code"):
            guard.check({"type": "item.completed", "item": {"type": "command_execution", "command": "x"}})


class McpContainmentTests(unittest.TestCase):
    def inspect(self, outputs):
        responses = [subprocess.CompletedProcess([], 0, json.dumps(output).encode(), b"") for output in outputs]
        with mock.patch.object(codex.subprocess, "run", side_effect=responses) as run:
            switches = codex.mcp_restrictions("codex", pathlib.Path("."), {})
        return switches, run.call_args_list

    def test_disables_discovered_servers_and_rechecks(self):
        switches, calls = self.inspect([[{"name": "node_repl", "enabled": True},
                                         {"name": "living-text", "enabled": False}],
                                        [{"name": "node_repl", "enabled": False},
                                         {"name": "living-text", "enabled": False}]])
        self.assertEqual(switches[:6], ["--disable", "plugins", "--disable", "apps", "--disable", "remote_plugin"])
        self.assertIn("mcp_servers.node_repl.enabled=false", switches)
        self.assertIn("mcp_servers.living-text.enabled=false", switches)
        self.assertEqual(calls[1].args[0], ["codex", "mcp", "list", "--json", *switches])

    def test_rejects_enabled_unexpected_or_unsafe_servers(self):
        first = [{"name": "server", "enabled": True}]
        for outputs in ([first, first], [first, [{"name": "new", "enabled": False}]],
                        [[{"name": 'bad.name', "enabled": True}]]):
            with self.subTest(outputs=outputs), self.assertRaises(ValueError):
                self.inspect(outputs)

    def test_provider_never_starts_if_preflight_fails(self):
        with mock.patch.object(codex, "codex_cli", return_value=("codex", "codex-cli 0.167.0")), \
                mock.patch.object(codex, "mcp_restrictions", side_effect=ValueError("fixture")), \
                mock.patch.object(codex, "supervise") as supervise:
            result = codex.run(pathlib.Path("."), "", 1, "attempt")
        self.assertFalse(result["ok"])
        self.assertIn("containment preflight failed", result["reason"])
        supervise.assert_not_called()

    def test_provider_receives_the_verified_restrictions(self):
        switches = ["--disable", "plugins", "-c", "mcp_servers.node_repl.enabled=false"]
        with mock.patch.object(codex, "codex_cli", return_value=("codex", "codex-cli 0.167.0")), \
                mock.patch.object(codex, "mcp_restrictions", return_value=switches), \
                mock.patch.object(codex, "supervise", return_value={"ok": True}) as supervise:
            result = codex.run(pathlib.Path("."), "brief", 1, "attempt")
        self.assertTrue(result["ok"])
        command = supervise.call_args.args[0]
        self.assertEqual(command[:2], ["codex", "exec"])
        self.assertEqual(command[2:2 + len(switches)], switches)
        self.assertIn("--approve-for-me", command)
        self.assertNotIn("--sandbox", command)
        self.assertIn("sandbox_workspace_write.network_access=false", command)
        self.assertEqual(result["cli_version"], "codex-cli 0.167.0")


class StopDeadlineTests(unittest.TestCase):
    def test_expired_member_snapshot_still_terminates_the_job(self):
        job = mock.MagicMock()
        job.members.return_value = range(100)
        job.active.return_value = 0
        process = mock.MagicMock()
        with mock.patch.object(codex.time, "monotonic", side_effect=[0, 11, 11, 11]):
            warning = codex.stop_tree(process, job)
        job.terminate.assert_called_once()
        job.pin.assert_not_called()
        self.assertIn("deadline expired", warning)

    def test_interruption_while_pinning_still_terminates_and_reports_uncertainty(self):
        job = mock.MagicMock()
        job.members.return_value = (123,)
        job.pin.side_effect = KeyboardInterrupt()
        job.active.return_value = 0
        warning = codex.stop_tree(mock.MagicMock(), job)
        job.terminate.assert_called_once()
        self.assertIn("could not pin job member", warning)

    def test_interruption_during_termination_retries_the_request_once(self):
        job = mock.MagicMock()
        job.members.return_value = ()
        job.terminate.side_effect = [KeyboardInterrupt(), None]
        job.active.return_value = 0
        warning = codex.stop_tree(mock.MagicMock(), job)
        self.assertEqual(job.terminate.call_count, 2)
        self.assertIn("interrupted while requesting", warning)

    @unittest.skipUnless(os.name == "nt", "Windows process handles")
    def test_interrupted_pin_closes_the_handle_it_opened(self):
        import ctypes
        job = object.__new__(codex.WindowsJob)
        job.ctypes = ctypes
        job.handle = 100
        job.kernel = mock.MagicMock()
        job.kernel.OpenProcess.return_value = 123
        job.kernel.IsProcessInJob.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            job.pin(42)
        job.kernel.CloseHandle.assert_called_once_with(123)


class FakeProcessTests(unittest.TestCase):
    """A fake provider emits the real JSONL shape and owns a real child process."""

    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="bg-codex-guard-"))
        self.cleanup_allowed = False
        self.env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_CONFIG_GLOBAL="NUL" if os.name == "nt" else "/dev/null",
                        GIT_CONFIG_NOSYSTEM="1", PYTHONDONTWRITEBYTECODE="1")
        self.git("init", "-q")
        (self.root / "source.txt").write_text("base\n", encoding="utf-8")
        (self.root / "fake_cli.py").write_text(
            'import json, os, pathlib, subprocess, sys, time\n'
            'args = {}\n'
            'if os.name == "nt":\n'
            '    info = subprocess.STARTUPINFO(); info.dwFlags |= subprocess.STARTF_USESHOWWINDOW; info.wShowWindow = subprocess.SW_HIDE\n'
            '    args = {"startupinfo": info, "creationflags": subprocess.CREATE_NO_WINDOW}\n'
            'pathlib.Path("logs").mkdir(exist_ok=True)\n'
            'if sys.argv[1] == "success":\n'
            '    pathlib.Path("logs/prompt.txt").write_bytes(sys.stdin.buffer.read())\n'
            '    print(json.dumps({"type":"thread.started", "thread_id":"fixture-thread"}), flush=True)\n'
            '    print(json.dumps({"type":"turn.completed", "usage":{"input_tokens":42}}), flush=True)\n'
            '    sys.exit(0)\n'
            'if sys.argv[1] == "success_child":\n'
            '    child = subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(8)"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **args)\n'
            '    pathlib.Path("logs/child.pid").write_text(str(child.pid), encoding="ascii")\n'
            '    print(json.dumps({"type":"turn.completed", "usage":{"input_tokens":42}}), flush=True)\n'
            '    sys.exit(0)\n'
            'child = subprocess.Popen([sys.executable, "-B", "fake_child.py"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **args)\n'
            'pathlib.Path("logs/child.pid").write_text(str(child.pid), encoding="ascii")\n'
            'pathlib.Path("logs/provider.pid").write_text(str(os.getpid()), encoding="ascii")\n'
            'for _ in range(40):\n'
            '    if pathlib.Path("logs/grandchild.pid").exists(): break\n'
            '    time.sleep(.05)\n'
            'print(json.dumps({"type":"thread.started", "thread_id":"fixture-thread"}), flush=True)\n'
            'if sys.argv[1] == "repeat":\n'
            '    for command, code in [("fail",1),("inspect",0),("fail",1),("inspect",0),("fail",1)]:\n'
            '        print(json.dumps({"type":"item.completed","item":{"type":"command_execution","command":command,"exit_code":code,"status":"failed" if code else "completed"}}), flush=True)\n'
            'time.sleep(8)\n', encoding="utf-8")
        (self.root / "fake_child.py").write_text(
            'import os, pathlib, subprocess, sys, time\n'
            'args = {}\n'
            'if os.name == "nt":\n'
            '    info = subprocess.STARTUPINFO(); info.dwFlags |= subprocess.STARTF_USESHOWWINDOW; info.wShowWindow = subprocess.SW_HIDE\n'
            '    args = {"startupinfo": info, "creationflags": subprocess.CREATE_NO_WINDOW}\n'
            'child = subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(8)"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **args)\n'
            'pathlib.Path("logs/grandchild.tmp").write_text(str(child.pid), encoding="ascii")\n'
            'pathlib.Path("logs/grandchild.tmp").replace("logs/grandchild.pid")\n'
            'time.sleep(8)\n', encoding="utf-8")
        self.git("add", "source.txt", "fake_cli.py", "fake_child.py")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")

    def tearDown(self):
        if not self.cleanup_allowed:
            print(f"Fixture preserved for inspection: {self.root}", file=sys.stderr)
            return
        target = self.root.resolve()
        parent = pathlib.Path(tempfile.gettempdir()).resolve()
        target.relative_to(parent)
        if target.parent != parent or not target.name.startswith("bg-codex-guard-"):
            raise AssertionError(f"refusing cleanup outside the fixture directory: {target}")
        def remove_readonly(func, name, error):
            pathlib.Path(name).resolve().relative_to(target)
            os.chmod(name, stat.S_IWRITE)
            func(name)
        if sys.version_info >= (3, 12):
            shutil.rmtree(target, onexc=remove_readonly)
        else:  # the KiCad machine's Python 3.9 names the same handler onerror
            shutil.rmtree(target, onerror=remove_readonly)

    def git(self, *args):
        result = subprocess.run(["git", *args], cwd=str(self.root), env=self.env, capture_output=True, check=False, **KWARGS)
        if result.returncode:
            raise AssertionError(result.stderr.decode("utf-8", "replace"))
        return result.stdout

    def assert_process_gone(self, label):
        pid = int((self.root / "logs" / f"{label}.pid").read_text(encoding="ascii"))
        if os.name == "nt":
            import ctypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.restype = ctypes.c_void_p
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                self.assertEqual(ctypes.get_last_error(), 87)  # process no longer exists
                return
            try:
                code = ctypes.c_ulong()
                self.assertTrue(kernel.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code)))
                self.assertNotEqual(code.value, 259)  # STILL_ACTIVE
            finally:
                kernel.CloseHandle(ctypes.c_void_p(handle))
        else:
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def assert_tree_gone(self):
        for label in ("provider", "child", "grandchild"):
            self.assert_process_gone(label)

    def shell_write(self, value):
        if os.name == "nt":
            command = ["cmd.exe", "/d", "/c", f"echo {value}>source.txt"]
        else:
            command = ["sh", "-c", f"printf '{value}\\n' > source.txt"]
        subprocess.run(command, cwd=str(self.root), env=self.env, check=True, **KWARGS)

    def test_actual_source_content_changes_the_fingerprint_but_reports_do_not(self):
        first = codex.source_fingerprint(self.root)
        (self.root / "source.txt").write_text("first change\n", encoding="utf-8")
        second = codex.source_fingerprint(self.root)
        self.git("add", "source.txt")
        self.assertEqual(codex.source_fingerprint(self.root), second)
        self.shell_write("second change")
        third = codex.source_fingerprint(self.root)
        self.assertEqual(len({first, second, third}), 3)
        report = self.root / "board/reports/example/report.txt"
        report.parent.mkdir(parents=True)
        report.write_text("timestamp 1", encoding="utf-8")
        self.assertEqual(codex.source_fingerprint(self.root), third)
        report.write_text("timestamp 2", encoding="utf-8")
        self.assertEqual(codex.source_fingerprint(self.root), third)
        log = self.root / "logs" / "example.log"
        log.parent.mkdir()
        log.write_text("timestamp 1", encoding="utf-8")
        self.assertEqual(codex.source_fingerprint(self.root), third)
        log.write_text("timestamp 2", encoding="utf-8")
        self.assertEqual(codex.source_fingerprint(self.root), third)
        self.cleanup_allowed = True

    def test_guard_cache_uses_same_sized_shell_edits_and_ignores_staging(self):
        self.shell_write("versionA")
        size = (self.root / "source.txt").stat().st_size
        guard = codex.RepeatGuard(self.root, 3)
        self.assertIsNone(guard.check(completed("fail", 1)))
        self.assertIsNone(guard.check(completed("fail", 1)))
        self.git("add", "source.txt")
        self.assertIsNone(guard.check(completed("inspect", 0)))
        self.assertIn("3 failures", guard.check(completed("fail", 1)))

        guard = codex.RepeatGuard(self.root, 3)
        self.assertIsNone(guard.check(completed("fail", 1)))
        self.assertIsNone(guard.check(completed("fail", 1)))
        self.shell_write("versionB")
        self.assertEqual((self.root / "source.txt").stat().st_size, size)
        self.assertIsNone(guard.check(completed("inspect", 0)))
        self.assertIsNone(guard.check(completed("fail", 1)))
        self.assertIsNone(guard.check(completed("fail", 1)))
        self.assertIn("3 failures", guard.check(completed("fail", 1)))
        self.cleanup_allowed = True

    def test_repeat_checkpoint_stops_the_fake_provider_and_its_child(self):
        events = self.root / "logs" / "events.jsonl"
        events.parent.mkdir()
        memberships = {}
        waited = set()
        original_terminate = codex.WindowsJob.terminate
        original_wait = codex.WindowsJob.wait_handle
        def verify_then_terminate(job):
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.IsProcessInJob.argtypes = (wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL))
            kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
            for label in ("provider", "child", "grandchild"):
                pid = int((self.root / "logs" / f"{label}.pid").read_text(encoding="ascii"))
                handle = kernel.OpenProcess(0x1000, False, pid)
                member = wintypes.BOOL()
                memberships[label] = bool(handle and kernel.IsProcessInJob(handle, job.handle, ctypes.byref(member))
                                          and member.value)
                if handle:
                    kernel.CloseHandle(handle)
            return original_terminate(job)

        def verify_wait(job, handle, seconds):
            import ctypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.GetProcessId.argtypes = (ctypes.c_void_p,)
            kernel.GetProcessId.restype = ctypes.c_ulong
            pid = kernel.GetProcessId(handle)
            result = original_wait(job, handle, seconds)
            if result:
                waited.add(pid)
            return result

        started = time.monotonic()
        patch = (mock.patch.object(codex.WindowsJob, "terminate", verify_then_terminate) if os.name == "nt" else
                 mock.patch.object(codex, "WINDOWS_BOOTSTRAP", codex.WINDOWS_BOOTSTRAP))
        wait_patch = (mock.patch.object(codex.WindowsJob, "wait_handle", verify_wait) if os.name == "nt" else
                      mock.patch.object(codex, "WINDOWS_BOOTSTRAP", codex.WINDOWS_BOOTSTRAP))
        with patch, wait_patch:
            result = codex.supervise([sys.executable, "-B", "fake_cli.py", "repeat"], self.root, "", 0.2,
                                     self.env, event_path=events)
        elapsed = time.monotonic() - started
        self.assertFalse(result["ok"])
        self.assertIn("3 failures of command sha256", result["reason"])
        self.assertEqual(result["termination_unconfirmed"], os.name == "nt")
        self.assertEqual(result["thread_id"], "fixture-thread")
        self.assertEqual(len([json.loads(line) for line in events.read_text().splitlines()]), 6)
        if os.name == "nt":
            self.assertEqual(memberships, {"provider": True, "child": True, "grandchild": True})
            expected = {int((self.root / "logs" / f"{label}.pid").read_text(encoding="ascii"))
                        for label in ("provider", "child", "grandchild")}
            self.assertTrue(expected.issubset(waited), (expected, waited))
        self.assertLess(elapsed, 8, "fixture processes must be stopped before their eight-second natural exit")
        self.assert_tree_gone()
        self.cleanup_allowed = True

    def test_wall_stop_stops_the_fake_provider_and_its_child(self):
        result = codex.supervise([sys.executable, "-B", "fake_cli.py", "timeout"], self.root, "", 0.03, self.env)
        self.assertFalse(result["ok"])
        self.assertIn("wall stop", result["reason"])
        self.assertEqual(result["termination_unconfirmed"], os.name == "nt")
        self.assert_tree_gone()
        self.cleanup_allowed = True

    def test_interruption_stops_the_fake_provider_and_its_child(self):
        with mock.patch.object(codex.RepeatGuard, "check", side_effect=KeyboardInterrupt):
            result = codex.supervise([sys.executable, "-B", "fake_cli.py", "repeat"], self.root, "", 0.2, self.env)
        self.assertFalse(result["ok"])
        self.assertIn("interrupted", result["reason"])
        self.assertEqual(result["termination_unconfirmed"], os.name == "nt")
        self.assert_tree_gone()
        self.cleanup_allowed = True

    def test_success_preserves_prompt_and_provider_usage(self):
        result = codex.supervise([sys.executable, "-B", "fake_cli.py", "success"], self.root,
                                 "fixture prompt", 0.2, self.env)
        self.assertTrue(result["ok"], result)
        self.assertEqual((result["provider_usage"], result["tokens"]), ({"input_tokens": 42}, 42))
        # Over the run: each turn's numbers added; the attempt's tokens are input (cached included) plus output.
        self.assertEqual(codex.tokens_used(codex.usage_sum({"input_tokens": 40, "output_tokens": 1},
                                                           {"input_tokens": 1, "cached_input_tokens": 30})), 42)
        self.assertIsNone(codex.tokens_used(codex.usage_sum(None, None)))
        self.assertEqual((self.root / "logs" / "prompt.txt").read_text(), "fixture prompt")
        self.assertFalse(result["termination_unconfirmed"])
        self.cleanup_allowed = True

    @unittest.skipUnless(os.name == "nt", "Windows job cleanup")
    def test_success_with_a_remaining_child_keeps_the_stop_unconfirmed(self):
        with mock.patch.object(codex.WindowsJob, "active", return_value=0):
            result = codex.supervise([sys.executable, "-B", "fake_cli.py", "success_child"], self.root,
                                     "fixture prompt", 0.2, self.env)
        self.assertFalse(result["ok"], result)
        self.assertTrue(result["termination_unconfirmed"], result)
        self.assertIn("stop-time job snapshot", result["reason"])
        self.assert_process_gone("child")
        self.cleanup_allowed = True

    @unittest.skipUnless(os.name == "nt", "Windows job identity handles")
    def test_unqueryable_job_member_keeps_stop_unconfirmed(self):
        count = len(codex._unconfirmed_ownership)
        with mock.patch.object(codex.WindowsJob, "pin", side_effect=OSError("fixture process access denied")):
            result = codex.supervise([sys.executable, "-B", "fake_cli.py", "timeout"], self.root, "", 0.03, self.env)
        self.assertTrue(result["termination_unconfirmed"], result)
        self.assertIn("could not pin job member", result["reason"])
        self.assert_tree_gone()
        self.assertEqual(len(codex._unconfirmed_ownership), count + 1)
        job, process, reader, writer = codex._unconfirmed_ownership.pop()
        reader.join(timeout=5)
        writer.join(timeout=5)
        self.assertFalse(reader.is_alive() or writer.is_alive())
        process.stdout.close()
        if not process.stdin.closed:
            process.stdin.close()
        job.close()
        self.cleanup_allowed = True

    @unittest.skipUnless(os.name == "nt", "Windows job assignment gate")
    def test_worker_never_starts_when_job_assignment_fails(self):
        with mock.patch.object(codex.WindowsJob, "assign", side_effect=OSError("fixture denied assignment")):
            result = codex.supervise([sys.executable, "-B", "fake_cli.py", "repeat"], self.root, "", 0.2, self.env)
        self.assertFalse(result["ok"])
        self.assertIn("fixture denied assignment", result["reason"])
        self.assertFalse((self.root / "logs" / "provider.pid").exists())
        self.cleanup_allowed = True

    def test_worker_stops_if_stream_thread_cannot_start(self):
        with mock.patch.object(codex.threading.Thread, "start", side_effect=RuntimeError("fixture thread failure")):
            result = codex.supervise([sys.executable, "-B", "fake_cli.py", "repeat"], self.root, "", 0.2, self.env)
        self.assertFalse(result["ok"])
        self.assertIn("fixture thread failure", result["reason"])
        self.assertEqual(result["termination_unconfirmed"], os.name == "nt")
        self.cleanup_allowed = True

    @unittest.skipUnless(os.name == "nt", "Windows job ownership")
    def test_query_failure_reports_unconfirmed_and_retains_job(self):
        count = len(codex._unconfirmed_ownership)
        with mock.patch.object(codex.WindowsJob, "active", side_effect=OSError("fixture query denied")):
            result = codex.supervise([sys.executable, "-B", "fake_cli.py", "timeout"], self.root, "", 0.03, self.env)
        self.assertEqual(len(codex._unconfirmed_ownership), count + 1)
        job, process, reader, writer = codex._unconfirmed_ownership[-1]
        try:
            if job.active():
                job.terminate()
            process.wait(timeout=5)
            reader.join(timeout=5)
            writer.join(timeout=5)
            self.assertFalse(reader.is_alive() or writer.is_alive())
            self.assertEqual(job.active(), 0)
            process.stdout.close()
            if not process.stdin.closed:
                process.stdin.close()
            job.close()
            codex._unconfirmed_ownership.pop()
        finally:
            if job.handle and job.active():
                job.terminate()
        self.assertTrue(result["termination_unconfirmed"], result)
        self.assertIn("fixture query denied", result["reason"])
        self.assert_tree_gone()
        self.cleanup_allowed = True


if __name__ == "__main__":
    unittest.main()
