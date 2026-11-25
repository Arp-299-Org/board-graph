"""Gemini (Antigravity CLI) operator and independent review for one locked KiCad editor step."""
from __future__ import annotations

import json
import math
import os
import pathlib
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time

import routing_codex
from no_window import KWARGS as NO_WINDOW

DEFAULT_MODEL = "gemini-4-neon-medium"
DEFAULT_REVIEW_MODEL = "gemini-4-neon-low"
KINDS = ("input", "output", "thinking", "cache_read", "cache_write")
OPERATOR_ENV = (
    "HOME", "USER", "PATH", "LANG", "LC_ALL", "TMPDIR",
    "DISPLAY", "XAUTHORITY",
    "SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY",
    "SYSTEMROOT", "TEMP", "TMP",
    "BOARD", "SOCKET", "KICAD_API_SOCKET", "ROUTING_HANDOFF_AT",
    "BOARD_GRAPH_PROJECT", "BOARD_GRAPH_BOARD", "BOARD_GRAPH_SITE", "BOARD_GRAPH_BOARDS",
    "KICAD_PYTHON", "KICAD_IPC_PYTHON",
)
REVIEW_ENV = (
    "HOME", "USER", "PATH", "LANG", "LC_ALL", "TMPDIR",
    "SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY",
    "SYSTEMROOT", "TEMP", "TMP",
)
OPERATOR_ALLOWED_TOOLS = frozenset({
    "run_command", "command_status", "wait", "wait_5_seconds",
    "read_file", "view_file", "list_dir", "find_by_name", "grep_search", "finish",
})
REVIEW_ALLOWED_TOOLS = frozenset({"read_file", "view_file", "finish"})
OPERATOR_DENY = ("write_file(*)", "edit_file(*)", "mcp(*)", "web(*)", "browser(*)", "subagent(*)")
REVIEW_DENY = ("command(*)", "write_file(*)", "edit_file(*)", "mcp(*)", "web(*)", "browser(*)", "subagent(*)")
MAX_INLINE_PROMPT_BYTES = 60_000
_REQUIRED_FLAGS = (
    "--print", "--output-format", "--model", "--effort",
    "--settings", "--disable-slash-commands", "--print-timeout",
)
_EXIT_CODE_RE = re.compile(r"Command exited with code (\d+)\.")


def preflight() -> str:
    """Inspect the installed Antigravity CLI (agy) without opening an editor or invoking a model."""
    found = shutil.which("agy")
    if found:
        binary = pathlib.Path(found)
    else:
        fallback = pathlib.Path.home() / ".local" / "bin" / "agy"
        if not fallback.is_file() or not os.access(fallback, os.X_OK):
            raise ValueError("Antigravity CLI (agy) was not found on the KiCad machine")
        binary = fallback
    try:
        proc = subprocess.run([str(binary), "--help"], capture_output=True, text=True, timeout=15, **NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(f"Cannot inspect the Antigravity CLI (agy): {error}") from error
    help_text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    missing = [flag for flag in _REQUIRED_FLAGS if not re.search(r"(?<![\w-])" + re.escape(flag) + r"(?![\w-])", help_text)]
    if proc.returncode or missing:
        raise ValueError(f"Installed Antigravity CLI (agy) cannot run the restricted routing profile: {', '.join(missing) or proc.stderr.strip()}")
    return str(binary.resolve())


def _settings(images, *, operator=False, extra_read_paths=()):
    """Build strict per-session settings JSON; return (settings_dict, resolved_allowed_paths)."""
    allowed = [pathlib.Path(image).resolve(strict=True) for image in images]
    allowed.extend(pathlib.Path(path).resolve(strict=True) for path in extra_read_paths)
    allowed_set = set(allowed)
    if operator:
        return {"permissions": {"allow": ["command(*)", "read_file(*)"], "deny": list(OPERATOR_DENY)}}, allowed_set
    return {"permissions": {"allow": [f"read_file({path})" for path in allowed], "deny": list(REVIEW_DENY)}}, allowed_set


def _argv(binary, model, effort, board, pid, settings_path, seconds, prompt):
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", model) or "opus" in model.lower():
        raise ValueError(f"A valid non-Opus Gemini/Antigravity model is required, got {model!r}")
    if pid is not None and (type(pid) is not int or pid <= 0 or not board or not pathlib.Path(board).is_absolute()):
        raise ValueError("The routing operator needs the live pid and absolute board path")
    timeout_s = max(1, int(math.ceil(seconds)))
    argv = [
        binary,
        "--print", prompt,
        "--output-format", "stream-json",
        "--model", model,
        "--disable-slash-commands",
        "--settings", str(settings_path),
        "--print-timeout", f"{timeout_s}s",
    ]
    if effort and not re.search(r"-(?:low|medium|high|xhigh|max)$", model, re.IGNORECASE):
        argv.extend(("--effort", str(effort)))
    return argv


def _usage(result):
    """Parse agy result usage. In agy 1.5+, input_tokens is already non-cached input."""
    if not isinstance(result, dict):
        return None
    usage = result.get("usage")
    if not isinstance(usage, dict):
        return None
    raw_in = usage.get("input_tokens")
    output = usage.get("output_tokens")
    cached = usage.get("cache_read_tokens", 0)
    thinking = usage.get("thinking_tokens")
    if any(type(n) is not int or n < 0 for n in (raw_in, output, cached)):
        return None
    if thinking is not None and (type(thinking) is not int or thinking < 0 or thinking > output):
        return None
    num_turns = result.get("num_turns")
    turns = num_turns if type(num_turns) is int and num_turns > 0 else 1
    return {
        "turns": turns,
        "input": raw_in,
        "output": output,
        "cache_read": cached,
        "thinking": thinking,
        "cache_write": None,
    }


def _lines(log):
    path = pathlib.Path(log)
    if path.is_file():
        with path.open(encoding="utf-8") as source:
            for line in source:
                try:
                    event = json.loads(line)
                except ValueError:
                    yield {}
                    continue
                yield event if isinstance(event, dict) else {}


def tokens(log, model):
    rounds, by_model = [], {}
    complete, started, accounted = True, False, False
    selected = model
    for event in _lines(log):
        kind = event.get("event")
        if kind == "routing.invocation_started":
            complete &= not started
            started, accounted = True, False
            selected = event.get("requested_model") or model
        elif kind == "result":
            res = event.get("result") or {}
            if res.get("status") != "SUCCESS":
                complete = False
                continue
            one = _usage(res)
            if one is None:
                complete = False
                continue
            accounted = True
            rounds.append(one)
            total = by_model.setdefault(selected, dict.fromkeys(KINDS))
            for key in KINDS:
                if one[key] is not None:
                    total[key] = (total[key] or 0) + one[key]
        elif kind == "routing.invocation_finished":
            complete &= started and accounted and event.get("metered", False)
            started = False
        elif not kind:
            complete = False
    return {
        "rounds": rounds,
        "by_model": by_model,
        "complete": bool(rounds and complete and not started),
        "model_source": "requested CLI model; reported init.model checked when available",
    }


def calls(log, board_stem: str | None = None):
    def _entries():
        for event in _lines(log):
            if event.get("event") != "step_update":
                continue
            step = event.get("step_update") or {}
            if step.get("step_type") != "tool" or step.get("tool_name") != "run_command":
                continue
            state = str(step.get("state") or "")
            if state not in ("DONE", "ERROR"):
                continue
            info = step.get("tool_info") or {}
            params = info.get("parameters") or {}
            command = str(params.get("CommandLine") or "").strip()
            if not command:
                continue
            raw_out = str(info.get("output") or "")
            match = _EXIT_CODE_RE.search(raw_out)
            exit_code = int(match.group(1)) if match else (1 if state == "ERROR" else 0)
            if "Command output:\n" in raw_out:
                output = raw_out.split("Command output:\n", 1)[1]
            else:
                output = raw_out
            item_status = "failed" if (state == "ERROR" or exit_code != 0) else "completed"
            duration_s = max(0.0, float(step.get("duration_seconds") or 0.0))
            yield (command, output, exit_code, item_status, duration_s)

    return routing_codex.audit_commands(_entries(), board_stem=board_stem)


def session_timing(log) -> dict:
    """Summarize Gemini wall seconds, tool execution seconds, LLM time and thinking tokens from an agy log.

    Prefers each invocation's terminal `result` summary, falling back per invocation to intermediate
    `step_update` usage and duration fields when an operator times out or is stopped before emitting `result`.
    """
    wall_s = 0.0
    tool_s = 0.0
    thinking_tokens = 0
    inv_result_wall: float | None = None
    inv_result_thinking: int | None = None
    inv_tool_dur: dict[object, float] = {}
    inv_step_dur: dict[object, float] = {}
    inv_step_thinking: dict[object, int] = {}
    anon_counter = 0

    def _flush() -> None:
        nonlocal wall_s, tool_s, thinking_tokens, inv_result_wall, inv_result_thinking
        tool_s += sum(inv_tool_dur.values())
        wall_s += inv_result_wall if inv_result_wall is not None else sum(inv_step_dur.values())
        thinking_tokens += inv_result_thinking if inv_result_thinking is not None else sum(inv_step_thinking.values())
        inv_result_wall = None
        inv_result_thinking = None
        inv_tool_dur.clear()
        inv_step_dur.clear()
        inv_step_thinking.clear()

    for event in _lines(log):
        kind = event.get("event")
        if kind == "routing.invocation_started":
            _flush()
        elif kind == "result":
            res = event.get("result") or {}
            if res.get("duration_seconds") is not None:
                inv_result_wall = (inv_result_wall or 0.0) + max(0.0, float(res["duration_seconds"]))
            usage = res.get("usage") or {}
            t = usage.get("thinking_tokens")
            if type(t) is int and t >= 0:
                inv_result_thinking = (inv_result_thinking or 0) + t
        elif kind == "step_update":
            step = event.get("step_update") or {}
            step_type = step.get("step_type")
            step_state = str(step.get("state") or "")
            step_idx = step.get("step_index")
            anon_counter += 1
            if step_type == "tool" and step_state in ("DONE", "ERROR"):
                dur = max(0.0, float(step.get("duration_seconds") or 0.0))
                inv_tool_dur[step_idx if step_idx is not None else ("t", anon_counter)] = dur
            if step_type in ("tool", "agent_response") and step_state in ("DONE", "ERROR"):
                dur = max(0.0, float(step.get("duration_seconds") or 0.0))
                inv_step_dur[step_idx if step_idx is not None else ("d", anon_counter)] = dur
            usage = step.get("usage")
            if isinstance(usage, dict):
                t = usage.get("thinking_tokens")
                if type(t) is int and t >= 0:
                    inv_step_thinking[step_idx if step_idx is not None else ("k", anon_counter)] = t
    _flush()
    llm_s = max(0.0, wall_s - tool_s)
    return {
        "wall_s": round(wall_s, 2),
        "tool_s": round(tool_s, 2),
        "llm_s": round(llm_s, 2),
        "thinking_tokens": thinking_tokens,
    }


def _record(sink, event):
    print(json.dumps(event, separators=(",", ":"), ensure_ascii=True), file=sink, flush=True)


def _step_violation(step: dict, operator: bool, allowed_read_paths) -> tuple[str, str]:
    info = step.get("tool_info") or {}
    tool_name = str(step.get("tool_name") or info.get("name") or "")
    allowed = OPERATOR_ALLOWED_TOOLS if operator else REVIEW_ALLOWED_TOOLS
    if tool_name not in allowed:
        return f"Unexpected tool in Gemini transcript: {tool_name}", tool_name
    if not operator and tool_name in ("read_file", "view_file"):
        params = info.get("parameters") or {}
        target = params.get("Path") or params.get("AbsolutePath") or ""
        if not target:
            return "Reviewer read unauthorized file: <missing path>", tool_name
        try:
            resolved = pathlib.Path(target).resolve()
        except OSError:
            return f"Reviewer read unauthorized file: {target}", tool_name
        if resolved not in allowed_read_paths:
            return f"Reviewer read unauthorized file: {target}", tool_name
    return "", tool_name


def _is_blocked_review_error(step: dict, tool_name: str) -> bool:
    if str(step.get("state") or "") != "ERROR":
        return False
    info = step.get("tool_info") or {}
    if str(info.get("output") or "").strip():
        return False
    err = info.get("error")
    if not isinstance(err, dict) or err.get("type") != "TOOL_ERROR":
        return False
    msg = str(err.get("message") or "").lower()
    permission_blocked = (
        "permission denied" in msg
        or "permission check failed" in msg
        or "deny rule" in msg
    )
    if tool_name not in REVIEW_ALLOWED_TOOLS:
        return permission_blocked
    return (
        permission_blocked
        or "file does not exist" in msg
        or "no such file" in msg
        or "not found" in msg
    )


def _event_error(event, model, operator, allowed_read_paths):
    kind = event.get("event", "")
    if kind == "init":
        reported = (event.get("init") or {}).get("model")
        if isinstance(reported, str) and reported and reported != model:
            return f"The provider changed the requested model {model}"
    elif kind == "step_update":
        step = event.get("step_update") or {}
        if step.get("step_type") == "tool":
            err_msg, _ = _step_violation(step, operator, allowed_read_paths)
            if err_msg:
                return err_msg
    elif kind == "result":
        res = event.get("result") or {}
        if res.get("status") != "SUCCESS":
            detail = res.get("error") or res.get("status") or "Gemini invocation failed"
            return str(detail)[:500]
        if res.get("denied_actions"):
            return "Gemini attempted a denied action"
    return ""


def _step_summary(event: dict) -> str:
    step = event.get("step_update") or {}
    if step.get("step_type") != "tool":
        return ""
    state = str(step.get("state") or "")
    tool = str(step.get("tool_name") or "")
    if state != "ACTIVE" or not tool:
        return ""
    if tool == "run_command":
        cmd = str(((step.get("tool_info") or {}).get("parameters") or {}).get("CommandLine") or "").strip()
        for name in ("kicad_lay.py", "kicad_route.py", "kicad_edit.py", "mac.py"):
            if name in cmd:
                return name
        return cmd.split()[0] if cmd else "run_command"
    return tool


def run(binary, prompt, model, effort, repo, env, log, seconds, *, pid=None, images=(), read_paths=(), on_status=None):
    """Run one new agy print-mode session, streaming JSONL events live to `log`."""
    operator = pid is not None
    state = {"reason": "", "answer": "", "metered": False}
    pending_review_violations: dict[object, str] = {}
    blocked_review_steps: set[object] = set()
    lock = threading.Lock()
    status_file = env.get("BOARD_GRAPH_STATUS_FILE") if isinstance(env, dict) else None

    def _emit_status(desc: str) -> None:
        if on_status is not None:
            on_status(desc)
        if status_file:
            try:
                pathlib.Path(status_file).write_text(f"{time.strftime('%H:%M:%S')} operator: {desc}\n", encoding="utf-8")
            except OSError:
                pass

    with pathlib.Path(log).open("a", encoding="utf-8") as sink, tempfile.TemporaryDirectory(prefix="routing-gemini-") as tmp:
        _record(sink, {"event": "routing.invocation_started", "requested_model": model, "role": "operator" if operator else "review"})
        try:
            root = pathlib.Path(tmp)
            extra_reads = list(read_paths)
            if len(prompt.encode("utf-8")) > MAX_INLINE_PROMPT_BYTES:
                prompt_file = root / "prompt.txt"
                prompt_file.write_text(prompt, encoding="utf-8")
                prompt_file = prompt_file.resolve(strict=True)
                extra_reads.append(prompt_file)
                cli_prompt = f"Read {prompt_file} completely using read_file and follow every instruction in it."
            else:
                cli_prompt = prompt
            settings_data, allowed_read_paths = _settings(images, operator=operator, extra_read_paths=extra_reads)
            settings_path = root / "settings.json"
            settings_path.write_text(json.dumps(settings_data), encoding="utf-8")
            argv = _argv(binary, model, effort, env.get("BOARD"), pid, settings_path, seconds, cli_prompt)
            allowed_env = OPERATOR_ENV if operator else REVIEW_ENV
            clean_env = {k: env[k] for k in allowed_env if k in env}
            work_dir = pathlib.Path(repo) if operator else root
            stdout_path = root / "stdout"
            stdout_path.touch()
            stop_tail = threading.Event()

            def _handle_raw(raw_line: bytes) -> None:
                if not raw_line:
                    return
                try:
                    event = json.loads(raw_line)
                    if not isinstance(event, dict) or not event.get("event") or str(event["event"]).startswith("routing."):
                        raise ValueError()
                except (UnicodeDecodeError, ValueError):
                    with lock:
                        state["reason"] = state["reason"] or "Gemini emitted invalid JSONL"
                    return
                _record(sink, event)
                kind = event.get("event", "")
                with lock:
                    if not operator and kind == "step_update":
                        step = event.get("step_update") or {}
                        if step.get("step_type") == "tool":
                            err_msg, tool_name = _step_violation(step, False, allowed_read_paths)
                            if err_msg:
                                step_idx = step.get("step_index")
                                step_key = (
                                    step_idx
                                    if step_idx is not None
                                    else ("anon", len(pending_review_violations) + len(blocked_review_steps))
                                )
                                step_state = str(step.get("state") or "")
                                if step_state == "ACTIVE":
                                    pending_review_violations[step_key] = err_msg
                                elif _is_blocked_review_error(step, tool_name):
                                    pending_review_violations.pop(step_key, None)
                                    blocked_review_steps.add(step_key)
                                else:
                                    pending_review_violations.pop(step_key, None)
                                    state["reason"] = state["reason"] or err_msg
                    elif not operator and kind == "result":
                        res = event.get("result") or {}
                        if res.get("status") != "SUCCESS":
                            detail = res.get("error") or res.get("status") or "Gemini invocation failed"
                            state["reason"] = state["reason"] or str(detail)[:500]
                        elif res.get("denied_actions"):
                            if not (blocked_review_steps and len(res["denied_actions"]) <= len(blocked_review_steps)):
                                state["reason"] = state["reason"] or "Gemini attempted a denied action"
                    else:
                        err_msg = _event_error(event, model, operator, allowed_read_paths)
                        state["reason"] = state["reason"] or err_msg
                    if kind == "result":
                        res = event.get("result") or {}
                        state["metered"] = res.get("status") == "SUCCESS" and _usage(res) is not None
                        if res.get("status") == "SUCCESS":
                            state["answer"] = str(res.get("response") or "")
                if event.get("event") == "step_update":
                    desc = _step_summary(event)
                    if desc:
                        try:
                            _emit_status(desc)
                        except Exception:
                            pass

            def _tail_stdout() -> None:
                with stdout_path.open("rb", buffering=0) as tail:
                    buf = b""
                    while not stop_tail.is_set():
                        chunk = tail.read(65536)
                        if not chunk:
                            stop_tail.wait(0.15)
                            continue
                        buf += chunk
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            _handle_raw(line + b"\n")
                    while True:
                        chunk = tail.read(65536)
                        if not chunk:
                            break
                        buf += chunk
                    for line in buf.splitlines():
                        _handle_raw(line)

            with stdout_path.open("w+b", buffering=0) as raw, (root / "stderr").open("w+b") as err:
                proc = subprocess.Popen(
                    argv, cwd=work_dir, env=clean_env, stdin=subprocess.DEVNULL,
                    stdout=raw, stderr=err, start_new_session=(os.name != "nt"),
                )
                reader = threading.Thread(target=_tail_stdout, daemon=True)
                reader.start()
                try:
                    proc.wait(timeout=seconds)
                except subprocess.TimeoutExpired:
                    try:
                        if hasattr(os, "killpg"):
                            os.killpg(proc.pid, signal.SIGKILL)
                        else:
                            proc.kill()
                    except ProcessLookupError:
                        pass
                    proc.wait()
                    with lock:
                        state["reason"] = (
                            "Operator did not hand back the editor within the handoff grace period"
                            if operator else "Independent review timed out"
                        )
                finally:
                    stop_tail.set()
                    reader.join(timeout=5)
                if not state["reason"] and pending_review_violations:
                    state["reason"] = next(iter(pending_review_violations.values()))
                if proc.returncode and not state["reason"]:
                    err.seek(max(0, err.tell() - 512))
                    state["reason"] = f"Gemini exited {proc.returncode}: {err.read().decode('utf-8', 'replace').strip()}"
        except (OSError, ValueError, TypeError) as error:
            state["reason"] = str(error)
        reason, answer, metered = state["reason"], state["answer"], state["metered"]
        if not metered or not answer.strip():
            reason = reason or "Gemini did not return a completed turn with token usage and a final answer"
        _record(sink, {"event": "routing.invocation_finished", "ok": not reason, "metered": metered, "error": reason})
    return not reason, reason or answer
