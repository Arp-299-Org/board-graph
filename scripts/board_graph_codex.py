"""Run one graph brief with the local Codex CLI; never choose a graph outcome."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

import graph_pack
import site_settings
from no_window import KWARGS as NO_WINDOW

MODEL = "gpt-6.1-terra"
EFFORT = "max"
# Neutral environment variable names passed to the worker.
ATTEMPT_ENV = "BOARD_GRAPH_ATTEMPT_ID"
RECORDS_ENV = "BOARD_GRAPH_RECORDS"
GRAPH_ENV = "BOARD_GRAPH_GRAPH"
CLI_ENV = "BOARD_GRAPH_CODEX_BIN"
STATE_ENV = "BOARD_GRAPH_STATE"  # board_graph.STATE_ENV; this module cannot import board_graph, which imports it
MIN_CLI = (0, 159, 0)  # GPT-6.1 Terra's documented minimum Codex CLI version.


def codex_cli(override=None):
    """Resolve a native CLI and its version without invoking a model or a shell wrapper."""
    override = override or site_settings.env(CLI_ENV)
    if override:
        binary = pathlib.Path(override)
        if not binary.is_absolute() or not binary.is_file() or binary.suffix.lower() in (".cmd", ".bat"):
            raise ValueError(f"{CLI_ENV} must name an existing absolute native executable")
    elif os.name == "nt" and (wrapper := shutil.which("codex.cmd")):
        node = shutil.which("node.exe")
        package = pathlib.Path(wrapper).parent / "node_modules" / "@openai" / "codex"
        if not node or not (package / "package.json").is_file():
            raise ValueError("the Codex Node wrapper is present but its Node executable or package is missing")
        expression = ("JSON.stringify({arch:process.arch,path:require.resolve("
                      "'@openai/codex-win32-'+process.arch+'/package.json',{paths:[process.argv[1]]})})")
        result = subprocess.run([node, "-p", expression, str(package)], capture_output=True, check=False,
                                timeout=15, **NO_WINDOW)
        if result.returncode:
            raise ValueError("the installed Codex wrapper could not resolve its native package")
        try:
            metadata = json.loads(result.stdout)
            target = {"x64": "x86_64-pc-windows-msvc", "arm64": "aarch64-pc-windows-msvc"}[metadata["arch"]]
            binary = pathlib.Path(metadata["path"]).parent / "vendor" / target / "bin" / "codex.exe"
        except (ValueError, KeyError, TypeError) as error:
            raise ValueError(f"the installed Codex package could not identify its native executable: {error}")
        if not binary.is_file():
            raise ValueError("the installed Codex package's native executable is missing")
    else:
        found = shutil.which("codex.exe" if os.name == "nt" else "codex")
        if not found:
            raise ValueError("Codex CLI was not found")
        binary = pathlib.Path(found)
    version = subprocess.run([str(binary), "--version"], capture_output=True, check=False, timeout=15, **NO_WINDOW)
    match = re.search(rb"codex-cli\s+(\d+)\.(\d+)\.(\d+)", version.stdout)
    if version.returncode or not match:
        raise ValueError(f"could not read the Codex CLI version at {binary}")
    numbers = tuple(int(part) for part in match.groups())
    if numbers < MIN_CLI:
        raise ValueError(f"Codex CLI at {binary} is {'.'.join(map(str, numbers))}; Terra needs 0.159.0 or newer")
    help_result = subprocess.run([str(binary), "exec", "--help"], capture_output=True, check=False,
                                 timeout=15, **NO_WINDOW)
    if help_result.returncode or b"--approve-for-me" not in help_result.stdout:
        raise ValueError(f"Codex CLI at {binary} does not support --approve-for-me")
    return str(binary.resolve()), version.stdout.decode("utf-8", "replace").strip()


def mcp_restrictions(executable, root, env):
    """Disable externally hosted tools for this invocation, and verify the resulting configuration."""
    switches = ["--disable", "plugins", "--disable", "apps", "--disable", "remote_plugin"]

    def servers(arguments):
        result = subprocess.run([executable, "mcp", "list", "--json", *arguments], cwd=str(root), env=env,
                                capture_output=True, check=False, timeout=30, **NO_WINDOW)
        if result.returncode:
            raise ValueError(f"MCP configuration inspection exited with code {result.returncode}")
        try:
            entries = json.loads(result.stdout)
        except (ValueError, UnicodeDecodeError) as error:
            raise ValueError("MCP configuration inspection did not return valid JSON") from error
        if not isinstance(entries, list):
            raise ValueError("MCP configuration inspection did not return a server list")
        found = {}
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("MCP configuration inspection returned an invalid server entry")
            name, enabled = entry.get("name"), entry.get("enabled")
            if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", name)
                    or type(enabled) is not bool or name in found):
                raise ValueError("MCP configuration inspection returned an unsafe or invalid server entry")
            found[name] = enabled
        return found

    initial = servers(switches)
    for name in sorted(initial):
        switches.extend(("-c", f"mcp_servers.{name}.enabled=false"))
    checked = servers(switches)
    if checked.keys() != initial.keys() or any(checked.values()):
        raise ValueError("MCP servers could not all be confirmed disabled for this run")
    return switches


# A job is retained here if Windows cannot confirm its members have stopped.
# Dropping the last handle would also drop the only way to inspect or stop it.
_unconfirmed_ownership = []


class WindowsJob:
    """Own a Windows process tree, including descendants created after assignment."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                        ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                        ("max_working_set", ctypes.c_size_t), ("active_limit", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                        ("scheduling", wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("basic", BasicLimits), ("io", IoCounters),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]

        class Accounting(ctypes.Structure):
            _fields_ = [("user_time", ctypes.c_longlong), ("kernel_time", ctypes.c_longlong),
                        ("period_user", ctypes.c_longlong), ("period_kernel", ctypes.c_longlong),
                        ("page_faults", wintypes.DWORD), ("total", wintypes.DWORD),
                        ("active", wintypes.DWORD), ("terminated", wintypes.DWORD)]

        self.ctypes, self.accounting = ctypes, Accounting
        self.kernel = kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel.QueryInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                     wintypes.DWORD, ctypes.c_void_p)
        kernel.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.IsProcessInJob.argtypes = (wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL))
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.handle = kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; no breakaway flags.
        if not kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign(self, process):
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise self.ctypes.WinError(self.ctypes.get_last_error())

    def active(self):
        data = self.accounting()
        if not self.kernel.QueryInformationJobObject(self.handle, 1, self.ctypes.byref(data),
                                                       self.ctypes.sizeof(data), None):
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        return data.active

    def terminate(self):
        if not self.kernel.TerminateJobObject(self.handle, 1):
            raise self.ctypes.WinError(self.ctypes.get_last_error())

    def members(self):
        """Snapshot the job's current members; do not mistake this for lifetime coverage."""
        from ctypes import wintypes

        capacity = 16
        while capacity <= 16384:
            class ProcessIds(self.ctypes.Structure):
                _fields_ = [("assigned", wintypes.DWORD), ("listed", wintypes.DWORD),
                            ("ids", self.ctypes.c_size_t * capacity)]
            data = ProcessIds()
            ok = self.kernel.QueryInformationJobObject(self.handle, 3, self.ctypes.byref(data),
                                                        self.ctypes.sizeof(data), None)
            error = self.ctypes.get_last_error() if not ok else 0
            if ok and data.assigned == data.listed and data.listed <= capacity:
                return tuple(int(data.ids[index]) for index in range(data.listed))
            if not ok and error not in (122, 234):
                raise self.ctypes.WinError(error)
            capacity = max(capacity * 2, data.assigned)
        raise OSError("the job member list exceeded the bounded snapshot")

    def pin(self, pid):
        """Keep one process identity alive while checking that it belongs to our job."""
        from ctypes import wintypes

        handle = self.kernel.OpenProcess(0x00100000 | 0x1000, False, pid)  # SYNCHRONIZE | QUERY_LIMITED_INFORMATION
        if not handle:
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        try:
            member = wintypes.BOOL()
            if not self.kernel.IsProcessInJob(handle, self.handle, self.ctypes.byref(member)):
                raise self.ctypes.WinError(self.ctypes.get_last_error())
            if not member.value:
                raise OSError(f"process {pid} changed job membership while being pinned")
        except BaseException:
            self.kernel.CloseHandle(handle)
            raise
        return handle

    def wait_handle(self, handle, seconds):
        result = self.kernel.WaitForSingleObject(handle, max(0, int(seconds * 1000)))
        if result == 0:
            return True
        if result == 258:
            return False
        raise self.ctypes.WinError(self.ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


# Wait for the parent to assign this bootstrap to its job before it starts the
# real worker. Reading exactly one byte leaves the rest of stdin for that worker.
WINDOWS_BOOTSTRAP = """import json, os, subprocess, sys
if os.read(0, 1) != b'1':
    sys.exit(125)
info = subprocess.STARTUPINFO()
info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
info.wShowWindow = subprocess.SW_HIDE
sys.exit(subprocess.call(json.loads(sys.argv[1]), stdin=sys.stdin.buffer, stdout=sys.stdout.buffer,
                         stderr=sys.stderr.buffer, creationflags=subprocess.CREATE_NO_WINDOW, startupinfo=info))
"""


def stop_tree(process, job):
    """Stop the tree; on Windows wait for each identity visible before termination."""
    errors = []
    pins = []
    end = time.monotonic() + 10
    if job is not None:
        job.members_at_stop = False
        try:
            before = job.members()
            job.members_at_stop = bool(before)
            for pid in before:
                if time.monotonic() >= end:
                    errors.append("the stop deadline expired while pinning job members")
                    break
                try:
                    pins.append(job.pin(pid))
                except BaseException as error:
                    errors.append(f"could not pin job member {pid}: {error}")
                    break
            if time.monotonic() < end:
                after = job.members()
                job.members_at_stop = job.members_at_stop or bool(after)
                if set(before) != set(after):
                    errors.append("job membership changed while members were being pinned")
            else:
                errors.append("job membership could not be rechecked before the stop deadline")
        except BaseException as error:
            errors.append(f"could not snapshot job members: {error}")
        try:
            job.terminate()
        except (KeyboardInterrupt, SystemExit) as error:
            errors.append(f"interrupted while requesting job termination: {type(error).__name__}")
            try:
                job.terminate()
            except BaseException as retry_error:
                errors.append(f"job termination could not be requested: {retry_error}")
        except BaseException as error:
            errors.append(str(error))
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as error:
            errors.append(str(error))
    try:
        process.wait(timeout=max(0, end - time.monotonic()))
    except subprocess.TimeoutExpired:
        errors.append("the worker did not exit")
    except BaseException as error:
        errors.append(f"could not wait for the worker: {error}")
    if job is not None:
        for handle in pins:
            try:
                if not job.wait_handle(handle, max(0, end - time.monotonic())):
                    errors.append("a pinned job member did not exit before the stop deadline")
            except BaseException as error:
                errors.append(f"could not wait for a pinned job member: {error}")
            finally:
                job.kernel.CloseHandle(handle)
    while True:
        try:
            if job is not None:
                active = job.active()
            else:
                try:
                    os.killpg(process.pid, 0)
                    active = 1
                except ProcessLookupError:
                    active = 0
            if not active:
                break
        except BaseException as error:
            errors.append(str(error))
            break
        if time.monotonic() >= end:
            errors.append(f"{active} job member(s) or process group still active")
            break
        time.sleep(0.05)
    if errors:
        return "Stopping the worker's descendants could not be confirmed: " + "; ".join(errors)
    return ""


# What a run writes, never its source: what the pack names (for a board: its reports, production and export folders
# and the editor steps' results), the graph's records and the logs.
SOURCE_EXCLUDES = (*(path.rstrip("/") + "/" for path in graph_pack.load().written),
                   "tools/board_graph_records", "logs/")  # every board's records


def source_fingerprint(root, cache=None):
    """Hash top-level working source contents without using index state as progress."""
    root = pathlib.Path(root).resolve()
    cache = {} if cache is None else cache

    def git(*args):
        result = subprocess.run(["git", *args], cwd=str(root), capture_output=True, check=False, timeout=30, **NO_WINDOW)
        if result.returncode:
            raise ValueError("git could not read the source inputs for the repeat checkpoint")
        return result.stdout

    def included(name):
        name = name.replace("\\", "/")
        return not any(name.startswith(prefix) for prefix in SOURCE_EXCLUDES) and not name.lower().endswith(
            (".log", ".jsonl", ".pyc", ".o", ".elf", ".hex", ".map", ".lst"))

    # HEAD paths keep a staged deletion in the same representation as an
    # unstaged deletion. The working file's bytes, not the index, are hashed.
    paths = set(filter(None, git("ls-files", "--cached", "--others", "--exclude-standard", "-z").split(b"\0")))
    paths.update(filter(None, git("ls-tree", "-r", "--name-only", "-z", "HEAD").split(b"\0")))
    names = set()
    for raw in paths:
        name = os.fsdecode(raw).replace("\\", "/")
        if not included(name):
            continue
        names.add(name)

    digest = hashlib.sha256()
    for name in sorted(names):
        path = root / name
        if path.is_symlink():
            marker = b"link\0" + os.fsencode(os.readlink(path))
        elif path.is_dir():
            # A submodule's own working tree and ignored files are outside the
            # observed inputs. An unobserved change can cause a conservative stop.
            marker = b"submodule"
        else:
            try:
                path.resolve().relative_to(root)
                before = path.stat()
                identity = (before.st_size, before.st_mtime_ns, before.st_ctime_ns,
                            before.st_ino, before.st_mode)
                cached = cache.get(path)
                if cached and cached[0] == identity:
                    content = cached[1]
                else:
                    with path.open("rb") as stream:
                        hashed = hashlib.sha256()
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            hashed.update(chunk)
                        content = hashed.digest()
                after = path.stat()
                if identity != (after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_ino, after.st_mode):
                    raise ValueError(f"source input changed while being read: {name}")
                cache[path] = (identity, content)
                marker = b"file\0" + str(after.st_mode & 0o111).encode() + b"\0" + content
            except FileNotFoundError:
                marker = b"missing"
            except (OSError, ValueError) as error:
                raise ValueError(f"could not fingerprint source input {name}: {error}") from error
        digest.update(os.fsencode(name) + b"\0" + marker + b"\0")
    return digest.hexdigest()


class RepeatGuard:
    """Count exact command failures per unchanged input fingerprint, including across intervening reads."""
    def __init__(self, root, limit, fingerprint=None):
        cache = {}
        self.root, self.limit = root, limit
        self.fingerprint = fingerprint or (lambda path: source_fingerprint(path, cache))
        self.failures = {}
        self.last_fingerprint = None

    def check(self, event):
        if event.get("type") != "item.completed" or event.get("item", {}).get("type") != "command_execution":
            return None
        item = event["item"]
        command, code = item.get("command"), item.get("exit_code")
        if not isinstance(command, str) or not command or type(code) is not int:
            raise ValueError("the provider did not supply a command and exit code for a completed command")
        current = self.fingerprint(self.root)
        if self.last_fingerprint is not None and self.last_fingerprint != current:
            self.failures.clear()
        self.last_fingerprint = current
        if code == 0:
            self.failures.pop(command, None)
            return None
        count = self.failures.get(command, 0) + 1
        self.failures[command] = count
        if count >= self.limit:
            command_hash = hashlib.sha256(command.encode("utf-8")).hexdigest()[:12]
            return f"{count} failures of command sha256 {command_hash} on unchanged source inputs"
        return None


def supervise(argv, root, prompt, minutes, env, repeat_failures=3, event_path=None):
    """Stream provider events, stop the process tree on the wall or repeat checkpoint, and return its observed result."""
    pending = queue.Queue()
    deadline = time.monotonic() + minutes * 60
    capture = None
    try:
        capture = open(event_path, "wb") if event_path is not None else None
        with tempfile.TemporaryFile() as stderr:
            job = WindowsJob() if os.name == "nt" else None
            command = [sys.executable, "-B", "-c", WINDOWS_BOOTSTRAP, json.dumps(argv)] if job else argv
            try:
                process = subprocess.Popen(command, cwd=str(root), env=env, stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE, stderr=stderr,
                                           **({} if job else {"start_new_session": True}), **NO_WINDOW)
            except BaseException:
                if job:
                    job.close()
                raise
            if job:
                try:
                    job.assign(process)
                except BaseException as error:
                    # The bootstrap has not received its start byte, so the worker cannot exist yet.
                    process.stdin.close()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        try:
                            process.kill()
                            process.wait(timeout=5)
                        except (OSError, subprocess.TimeoutExpired):
                            pass
                    unconfirmed = process.poll() is None
                    if unconfirmed:
                        _unconfirmed_ownership.append((job, process, None, None))
                    else:
                        process.stdout.close()
                        job.close()
                    return {"ok": False, "tokens": None, "termination_unconfirmed": unconfirmed,
                            "reason": f"Codex job assignment failed: {type(error).__name__}: {error}"
                                      + (". Bootstrap termination could not be confirmed" if unconfirmed else "")}

            def read_events():
                try:
                    for line in process.stdout:
                        pending.put(line)
                finally:
                    pending.put(None)

            def send_prompt():
                try:
                    process.stdin.write((b"1" if job else b"") + prompt.encode("utf-8"))
                    process.stdin.close()
                except (OSError, ValueError):
                    pass

            reader = threading.Thread(target=read_events, daemon=True)
            writer = threading.Thread(target=send_prompt, daemon=True)
            try:
                reader.start()
                writer.start()
            except BaseException as error:
                warning = stop_tree(process, job)
                historical = "the stop-time job snapshot cannot prove every earlier member exited" if job else ""
                if reader.ident is not None:
                    reader.join(timeout=5)
                if writer.ident is not None:
                    writer.join(timeout=5)
                if reader.is_alive() or writer.is_alive():
                    warning = (warning + "; " if warning else "") + "provider pipes did not close"
                if warning:
                    _unconfirmed_ownership.append((job, process, reader, writer))
                else:
                    process.stdout.close()
                    if not process.stdin.closed:
                        process.stdin.close()
                    if job:
                        job.close()
                warning = "; ".join(filter(None, (warning, historical)))
                return {"ok": False, "tokens": None, "termination_unconfirmed": bool(warning),
                        "reason": f"Codex supervisor could not start its pipes: {type(error).__name__}: {error}"
                                  + (". " + warning if warning else "")}
            guard, thread_id, usage, provider_error = RepeatGuard(root, repeat_failures), None, None, None
            reason, unconfirmed, done = None, False, False
            try:
                while not done:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        reason = f"Codex exceeded the {minutes}-minute wall stop"
                        break
                    try:
                        raw = pending.get(timeout=min(0.25, remaining))
                    except queue.Empty:
                        continue
                    if raw is None:
                        done = True
                        continue
                    if capture:
                        capture.write(raw)
                        capture.flush()
                    try:
                        event = json.loads(raw)
                    except (ValueError, UnicodeDecodeError):
                        reason = "the provider emitted invalid JSON, so the repeat checkpoint cannot observe it"
                        break
                    if event.get("type") == "thread.started":
                        thread_id = event.get("thread_id")
                    elif event.get("type") == "turn.completed":
                        usage = usage_sum(usage, event.get("usage"))
                    elif event.get("type") == "turn.failed":
                        provider_error = event.get("error")
                    try:
                        reason = guard.check(event)
                    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                        reason = f"the repeat checkpoint could not inspect its inputs: {error}"
                    if reason:
                        break
                if not reason:
                    try:
                        process.wait(timeout=max(0.01, deadline - time.monotonic()))
                    except subprocess.TimeoutExpired:
                        reason = f"Codex exceeded the {minutes}-minute wall stop"
                if not reason and job:
                    # Job accounting can briefly retain an exiting process after wait().
                    # Give it a bounded grace period before declaring a leaked child.
                    grace = min(deadline, time.monotonic() + 1)
                    while job.active() and time.monotonic() < grace:
                        time.sleep(0.025)
                    if job.active():
                        reason = "Codex exited while owned child processes remained"
                elif not reason and not job:
                    try:
                        os.killpg(process.pid, 0)
                    except ProcessLookupError:
                        pass
                    else:
                        reason = "Codex exited while owned child processes remained"
            except KeyboardInterrupt:
                reason = "Codex was interrupted"
            except Exception as error:
                reason = f"The Codex supervisor failed: {type(error).__name__}: {error}"
            finally:
                warning = stop_tree(process, job)
                historical = ("the stop-time job snapshot cannot prove every earlier member exited"
                              if job is not None and (reason is not None or job.members_at_stop) else "")
                reader.join(timeout=5)
                writer.join(timeout=5)
                if reader.is_alive() or writer.is_alive():
                    warning = (warning + "; " if warning else "") + "provider pipes did not close"
                if warning:
                    unconfirmed = True
                    reason = (reason + ". " if reason else "") + warning
                    # Keep the owner and its pipes reachable if anything may still be running.
                    _unconfirmed_ownership.append((job, process, reader, writer))
                else:
                    process.stdout.close()
                    if not process.stdin.closed:
                        process.stdin.close()
                    if job:
                        job.close()
                if historical:
                    unconfirmed = True
                    reason = (reason + ". " if reason else "") + historical
            stopped = reason is not None
            if reason is None:
                reason = f"Codex exited with code {process.returncode}"
                if provider_error:
                    reason += ": " + json.dumps(provider_error, sort_keys=True)[:600]
                elif process.returncode:
                    stderr.seek(0, os.SEEK_END)
                    stderr.seek(max(0, stderr.tell() - 2000))
                    diagnostic = stderr.read().decode("utf-8", "replace").strip()
                    if diagnostic:
                        reason += ": " + " ".join(diagnostic.split())[-600:]
            return {"reason": reason, "tokens": tokens_used(usage),
                    "ok": done and process.returncode == 0 and not provider_error and not unconfirmed and not stopped,
                    "thread_id": thread_id, "provider_usage": usage, "provider_error": provider_error,
                    "termination_unconfirmed": unconfirmed}
    except OSError as error:
        return {"reason": f"Codex could not start: {type(error).__name__}: {error}", "tokens": None, "ok": False}
    finally:
        if capture:
            capture.close()


def usage_sum(total, turn):
    """The provider's usage over the run: each completed turn's numbers added to the total (None until one reports)."""
    if not isinstance(turn, dict):
        return total
    total = dict(total or {})
    for key, value in turn.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            total[key] = total.get(key, 0) + value
    return total


def tokens_used(usage):
    """The attempt's tokens: the provider's input and output totals for the run (cached input is part of input), None
    when it reported none."""
    if not usage:
        return None
    return int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))


def run(root, prompt, minutes, attempt_id, state_path=None, records_path=None, graph_path=None, event_path=None,
        executable=None, repeat_failures=None):
    """Run one Codex worker. Its tokens are the provider's input and output totals for the run, kept on the attempt."""
    try:
        executable, cli_version = codex_cli(executable)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        return {"reason": f"Codex CLI selection failed: {error}", "tokens": None, "ok": False}
    env = dict(os.environ, **site_settings.both_names(ATTEMPT_ENV, attempt_id))
    if state_path is not None:
        env.update(site_settings.both_names(STATE_ENV, str(pathlib.Path(state_path).resolve())))
    if records_path is not None:
        env.update(site_settings.both_names(RECORDS_ENV, str(pathlib.Path(records_path).resolve())))
    if graph_path is not None:
        env.update(site_settings.both_names(GRAPH_ENV, str(pathlib.Path(graph_path).resolve())))
    try:
        restrictions = mcp_restrictions(executable, root, env)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        return {"reason": f"Codex MCP containment preflight failed: {error}", "tokens": None, "ok": False,
                "executable": executable, "cli_version": cli_version, "termination_unconfirmed": False}
    argv = [executable, "exec", *restrictions, "-m", MODEL, "-c", f'model_reasoning_effort="{EFFORT}"',
            "--approve-for-me", "-c", "sandbox_workspace_write.network_access=false", "--json", "-C", str(root), "-"]
    result = supervise(argv, root, prompt, minutes, env, repeat_failures or 3, event_path)
    result.update(executable=executable, cli_version=cli_version)
    return result
