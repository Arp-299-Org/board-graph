"""Codex operator and independent review for one locked KiCad editor step."""
from __future__ import annotations

import collections
import json
import os
import pathlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile

from no_window import KWARGS as NO_WINDOW

OPERATOR_DISABLED = ("multi_agent", "code_mode", "plugins", "remote_plugin", "apps", "hooks",
                     "browser_use", "computer_use", "artifact", "image_generation", "goals")
DISABLED = ("shell_tool", "unified_exec", "view_image", *OPERATOR_DISABLED)
KINDS = ("input", "output", "thinking", "cache_read", "cache_write")
OPERATOR_ENV = ("HOME", "USER", "PATH", "LANG", "LC_ALL", "TMPDIR", "DISPLAY", "XAUTHORITY", "CODEX_HOME", "OPENAI_API_KEY",
                "SYSTEMROOT", "TEMP", "TMP", "BOARD", "SOCKET", "KICAD_API_SOCKET", "ROUTING_HANDOFF_AT",
                "BOARD_GRAPH_PROJECT", "BOARD_GRAPH_BOARD", "BOARD_GRAPH_SITE", "BOARD_GRAPH_BOARDS",
                "KICAD_PYTHON", "KICAD_IPC_PYTHON")
REVIEW_ENV = ("HOME", "USER", "PATH", "LANG", "LC_ALL", "TMPDIR", "CODEX_HOME", "OPENAI_API_KEY",
              "SYSTEMROOT", "TEMP", "TMP")


def preflight() -> str:
    """Inspect the installed CLI without opening an editor or invoking a model."""
    binary = shutil.which("codex")
    if not binary:
        raise ValueError("Codex CLI was not found on the KiCad machine")
    for args, required in ((["exec", "--help"], ("--ephemeral", "--ignore-user-config", "--ignore-rules", "--strict-config",
                                                "--skip-git-repo-check", "--sandbox", "--json", "--image")),
                           (["features", "list"], DISABLED)):
        try:
            proc = subprocess.run([binary, *args], capture_output=True, text=True, timeout=15, **NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ValueError(f"Cannot inspect the Codex CLI: {error}") from error
        missing = [flag for flag in required if not re.search(r"(?<![\w-])" + re.escape(flag) + r"(?![\w-])", proc.stdout)]
        if proc.returncode or missing:
            raise ValueError(f"Installed Codex CLI cannot run the restricted routing profile: {', '.join(missing) or proc.stderr.strip()}")
    return str(pathlib.Path(binary).resolve())


def _catalog(env, model, destination, *, operator=False):
    """Keep the selected model's real image and reasoning metadata; keep shell for the operator, remove it for review."""
    if not re.fullmatch(r"gpt-[A-Za-z0-9._-]+", model or ""):
        raise ValueError(f"An explicit OpenAI model is required, got {model!r}")
    source = pathlib.Path(env.get("CODEX_HOME") or pathlib.Path.home() / ".codex") / "models_cache.json"
    try:
        models = json.loads(source.read_text(encoding="utf-8"))["models"]
        entry, = [m for m in models if m.get("slug") == model]
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise ValueError(f"The Mac's Codex model catalog has no usable entry for {model} ({source})") from error
    if "image" not in entry.get("input_modalities", []):
        raise ValueError(f"{model} does not advertise image input for the PCB Editor")
    # Remove delegation, patch writers and web search; keep the model's native shell for the operator only.
    entry.update(apply_patch_tool_type=None, experimental_supported_tools=[],
                 multi_agent_version=None, node_repl_disabled=True, supports_search_tool=False)
    if not operator:
        entry.update(shell_type="disabled", tool_mode="standard")
    destination.write_text(json.dumps({"models": [entry]}), encoding="utf-8")


def _argv(binary, model, effort, repo, board, pid, catalog, images, handoff_at=None):
    operator = pid is not None
    if operator and (type(pid) is not int or pid <= 0 or not board or not pathlib.Path(board).is_absolute()):
        raise ValueError("The routing operator needs the live pid and absolute board path")
    config = ["-c", "model_provider=\"openai\"", "-c", "web_search=\"disabled\"",
              "-c", "project_doc_max_bytes=0", "-c", "model_catalog_json=" + json.dumps(str(catalog)),
              "-c", "mcp_servers={}"]
    for name in (OPERATOR_DISABLED if operator else DISABLED):
        config.extend(("--disable", name))
    if effort:
        config.extend(("-c", "model_reasoning_effort=" + json.dumps(effort)))
    sandbox = "danger-full-access" if operator else "read-only"
    argv = [binary, "-a", "never", "exec", "--strict-config", "--json", "--ephemeral", "--skip-git-repo-check",
            "--ignore-user-config", "--ignore-rules", "--sandbox", sandbox, "--model", model, *config]
    for image in images:
        argv.extend(("--image", str(pathlib.Path(image).resolve(strict=True))))
    return argv + ["--", "-"]


def _usage(event):
    usage = event.get("usage")
    if not isinstance(usage, dict):
        return None
    raw, output, cached = (usage.get(key) for key in ("input_tokens", "output_tokens", "cached_input_tokens"))
    write = usage.get("cache_write_input_tokens")
    thinking = usage.get("reasoning_output_tokens")
    if any(type(n) is not int or n < 0 for n in (raw, output, cached)):
        return None
    if any(type(n) is not int or n < 0 for n in (write, thinking) if n is not None):
        return None
    if cached + (write or 0) > raw or (thinking or 0) > output:
        return None
    # Graph input categories are disjoint. Their sum must equal the CLI's input_tokens.
    return {"turns": 1, "input": raw - cached - (write or 0), "output": output,
            "cache_read": cached, "thinking": thinking, "cache_write": write}


def _lines(log):
    if log.is_file():
        with log.open(encoding="utf-8") as source:
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
        kind = event.get("type")
        if kind == "routing.invocation_started":
            complete &= not started
            started, accounted = True, False
            selected = event["requested_model"]
        elif kind == "turn.completed":
            one = _usage(event)
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
    return {"rounds": rounds, "by_model": by_model, "complete": bool(rounds and complete and not started),
            "model_source": "requested CLI model; reported model checked when available"}


def _route_status(text: str) -> str:
    return _tool_status(text, "kicad_route.py")


def _parse_tool_statuses(text: str) -> list[tuple[str, str, str]]:
    """Parse (verb, status, reason) tuples from JSON status lines in command output."""
    out: list[tuple[str, str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                found = json.loads(line)
            except ValueError:
                continue
            if isinstance(found, dict) and found.get("status"):
                verb = str(found.get("verb") or "")
                status = str(found["status"])
                raw_reason = str(found.get("reason") or "").strip()
                out.append((verb, status, " ".join(raw_reason.split())[:220]))
    return out


def _tool_status_and_reason(text: str, script_name: str, *, ignore_verbs: tuple[str, ...] = ()) -> tuple[str, str]:
    matched = [
        (status, reason)
        for verb, status, reason in _parse_tool_statuses(text)
        if not (ignore_verbs and verb in ignore_verbs)
    ]
    if matched:
        return matched[-1]
    return ("usage" if f"usage: {script_name}" in text else "no_status"), ""


def _consume_lay_status(
    statuses: list[tuple[str, str, str]],
    used: set[int],
    verb: str,
    text: str,
    prior_refused: bool,
) -> tuple[str, str]:
    """Match one chained kicad_lay.py invocation against JSON status lines by verb and order."""
    candidates = [
        (idx, jverb, st, reason)
        for idx, (jverb, st, reason) in enumerate(statuses)
        if idx not in used and jverb not in ("list", "delete", "move-via", "move-text")
    ]
    chosen = next((c for c in candidates if verb != "none" and c[1] == verb), None)
    if chosen is None and verb == "none" and candidates:
        chosen = candidates[0]
    if chosen is None:
        chosen = next((c for c in candidates if c[1] == ""), None)
    if chosen is not None:
        used.add(chosen[0])
        return chosen[2], chosen[3]
    if "usage: kicad_lay.py" in text:
        return "usage", ""
    if prior_refused:
        return "skipped", ""
    return "no_status", ""


_NET_TOK = r"(?:[^\s()]+|\([^()]*\))+"
_DIRECTIONS_RE = re.compile(r"\bdirections\s+\[[^\]]*(?:\]|$)")
_PAREN_COORD_RE = re.compile(r"\(\s*-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?\s*\)")
_BRACKET_COORD_RE = re.compile(r"\[\s*-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?\s*\]")
_PAD_REF_RE = re.compile(r"\b[A-Za-z_]{1,8}\d+\.[A-Za-z0-9_]+\b")
_DRC_NETS_RE = re.compile(rf"\(nets\s+{_NET_TOK}\s+and\s+{_NET_TOK}\)")
_DRC_CLEARANCE_RE = re.compile(r"\(clearance\s+[0-9.]+\s*mm;\s*actual\s+-?[0-9.]+\s*mm\)")
_GAP_NEED_RE = re.compile(r"\(gap=\s*-?[0-9.]+\s*mm,\s*need\s+[0-9.]+\s*mm\)")
_SIZE_EQ_RE = re.compile(r"\b([wd])=[0-9.]+\s*mm\b")
_NET_ON_RE = re.compile(
    rf"\bon\s+(?!(?:board|read-back|this|the|a|an|any|another|each|existing|one|some|F\.Cu|B\.Cu|In\d+\.Cu|<net>)\b|\(<x,y>\)|\[<x,y>\]){_NET_TOK}"
)
_OBSTACLE_NET_RE = re.compile(
    rf"\b(planned|batch|existing)\s+(?!<net>\b|<[^>]+>){_NET_TOK}(?=\s+(?:track|via|copper|pour|on\b|<|\(|(?:F|B|In\d+)\.Cu))"
)
_PAREN_NET_RE = re.compile(rf"(<pad>|(?:F|B|In\d+)\.Cu)\s+\({_NET_TOK}\)")


def normalize_refusal_reason(reason_key: str) -> str:
    """Normalize pad, net, coordinate, clearance and direction literals in a refusal key for cross-step grouping."""
    text = str(reason_key or "").strip()
    text = _DIRECTIONS_RE.sub("directions [...]", text)
    text = _PAREN_COORD_RE.sub("(<x,y>)", text)
    text = _BRACKET_COORD_RE.sub("[<x,y>]", text)
    text = _PAD_REF_RE.sub("<pad>", text)
    text = _DRC_NETS_RE.sub("(nets <net> and <net>)", text)
    text = _DRC_CLEARANCE_RE.sub("(clearance <mm>; actual <mm>)", text)
    text = _GAP_NEED_RE.sub("(gap=<mm>, need <mm>)", text)
    text = _SIZE_EQ_RE.sub(r"\1=<mm>", text)
    text = _PAREN_NET_RE.sub(r"\1 (<net>)", text)
    text = _OBSTACLE_NET_RE.sub(r"\1 <net>", text)
    text = _NET_ON_RE.sub("on <net>", text)
    return " ".join(text.split())


def other_command_head(command: str, parts: list[list[str]] | None = None) -> str:
    """Return the executable basename for an unmatched shell command."""
    if parts is None:
        parts = _command_parts(command)
    for part in parts:
        skip_flags = False
        for tok in part:
            if "=" in tok and not tok.startswith("-"):
                key, _, _val = tok.partition("=")
                if key and key.isidentifier():
                    continue
            if skip_flags and tok.startswith("-"):
                continue
            name = pathlib.PurePosixPath(tok.replace("\\", "/")).name
            if not name:
                continue
            if name == "env":
                skip_flags = True
                continue
            return name
    return "unknown"


def summarize_extra_tools(step_calls: list[dict]) -> dict | None:
    """Aggregate passive kicad_edit verb counts and unmatched shell command heads across steps."""
    edit_by_verb: collections.Counter = collections.Counter()
    other_by_cmd: collections.Counter = collections.Counter()
    for calls in step_calls:
        if not isinstance(calls, dict):
            continue
        for verb, cnt in (calls.get("edit_by_verb") or {}).items():
            if int(cnt or 0) > 0:
                edit_by_verb[str(verb)] += int(cnt)
        for cmd, cnt in (calls.get("other_by_cmd") or {}).items():
            if int(cnt or 0) > 0:
                other_by_cmd[str(cmd)] += int(cnt)
    out: dict = {}
    if edit_by_verb:
        out["edit_by_verb"] = {k: edit_by_verb[k] for k in sorted(edit_by_verb)}
    if other_by_cmd:
        out["other_by_cmd"] = {k: other_by_cmd[k] for k in sorted(other_by_cmd)}
    return out or None


def summarize_refusals(step_calls: list[dict]) -> dict | None:
    """Aggregate per-step raw refusal reasons alongside normalized cross-step repeat classes."""
    help_calls = sum(int(calls.get("help_calls") or 0) for calls in step_calls)
    refusal_seconds = round(sum(float(calls.get("refusal_seconds") or 0.0) for calls in step_calls), 2)
    by_reason: dict[str, dict] = {}
    by_class: dict[str, dict] = {}
    for calls in step_calls:
        seen_raw: set[str] = set()
        seen_cls: set[str] = set()
        for reason_key, entry in (calls.get("refusal_reasons") or {}).items():
            if not isinstance(entry, dict):
                continue
            cnt = int(entry.get("count") or 0)
            sec = float(entry.get("seconds") or 0.0)
            raw_b = by_reason.setdefault(reason_key, {"count": 0, "seconds": 0.0, "steps": 0})
            raw_b["count"] += cnt
            raw_b["seconds"] += sec
            if reason_key not in seen_raw:
                raw_b["steps"] += 1
                seen_raw.add(reason_key)
            cls_key = normalize_refusal_reason(reason_key)
            cls_b = by_class.setdefault(cls_key, {"count": 0, "seconds": 0.0, "steps": 0})
            cls_b["count"] += cnt
            cls_b["seconds"] += sec
            if cls_key not in seen_cls:
                cls_b["steps"] += 1
                seen_cls.add(cls_key)
    if not help_calls and not by_reason:
        return None
    ordered = sorted(by_reason.items(), key=lambda kv: (-kv[1]["count"], kv[0]))
    return {
        "help_calls": help_calls,
        "refusal_seconds": refusal_seconds,
        "by_reason": {
            k: {"count": v["count"], "seconds": round(v["seconds"], 2), "steps": v["steps"]}
            for k, v in ordered
        },
        "repeated_across_steps": [
            {"reason": k, "steps": v["steps"], "count": v["count"], "seconds": round(v["seconds"], 2)}
            for k, v in sorted(by_class.items(), key=lambda kv: (-kv[1]["steps"], -kv[1]["count"], kv[0]))
            if v["steps"] >= 2
        ],
    }


def _tool_status(text: str, script_name: str, *, ignore_verbs: tuple[str, ...] = ()) -> str:
    return _tool_status_and_reason(text, script_name, ignore_verbs=ignore_verbs)[0]


def _tool_reason(text: str, script_name: str, *, ignore_verbs: tuple[str, ...] = ()) -> str:
    return _tool_status_and_reason(text, script_name, ignore_verbs=ignore_verbs)[1]


def _command_words(command: str) -> list[str]:
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    # Codex CLI wraps shell commands in "/bin/zsh -lc <cmd>" or "sh -c <cmd>".
    if len(words) >= 3 and pathlib.PurePosixPath(words[0]).name in ("sh", "bash", "zsh") and words[1] in ("-c", "-lc"):
        inner = words[2]
        try:
            return shlex.split(inner)
        except ValueError:
            return inner.split()
    return words


def _is_python(words: list[str], idx: int) -> bool:
    if idx <= 0:
        return False
    prev = pathlib.PurePosixPath(words[idx - 1]).name.lower()
    if "python" in prev or prev == "py":
        return True
    return idx >= 2 and words[idx - 1] == "-3" and pathlib.PurePosixPath(words[idx - 2]).name.lower() == "py"


def _script_index(words: list[str], name: str) -> int | None:
    suffix = f"scripts/{name}"
    for idx, word in enumerate(words):
        if (word == suffix or word.endswith("/" + suffix)) and _is_python(words, idx):
            return idx
    return None


def _edit_verb(words: list[str]) -> str:
    idx = 0
    while idx < len(words):
        word = words[idx]
        if word == "--pid":
            idx += 2
            continue
        if word.startswith("-"):
            idx += 1
            continue
        if word in ("\u2026", "...") or word.endswith("\u2026") or word.endswith("..."):
            return "none"
        return word
    return "none"


def _command_parts(command: str) -> list[list[str]]:
    words = _command_words(command)
    parts: list[list[str]] = [[]]
    for word in words:
        if word in (";", "&&", "||"):
            if parts[-1]:
                parts.append([])
            continue
        if word.endswith(";") and len(word) > 1:
            parts[-1].append(word[:-1])
            parts.append([])
            continue
        parts[-1].append(word)
    return [part for part in parts if part]


def _valid_editor_look(after: list[str], output: str, exit_code, status: str, board_stem: str | None = None) -> bool:
    """True only when mac.py look exited 0, targeted the assigned PCB Editor (or whole screen), and wrote a PNG."""
    if exit_code != 0 or status == "failed":
        return False
    args = after[1:]
    target = None
    idx = 0
    while idx < len(args):
        word = args[idx]
        if word in ("--width", "--out"):
            idx += 2
            continue
        if word.startswith("-"):
            idx += 1
            continue
        target = word
        break
    if target is not None and not (target.startswith("#") or "pcb" in target.lower() or "kicad" in target.lower()):
        return False
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not any(line.lower().endswith(".png") for line in lines):
        return False
    for line in lines:
        match = re.search(r"px of window #\d+ \(([^:]+):\s*(.+)\);\s*1 px =", line)
        if match:
            app, title = match[1].strip(), match[2].strip()
            if app != "PCB Editor":
                return False
            if board_stem and title.removeprefix("*") not in (
                    f"{board_stem} \u2014 PCB Editor", f"{board_stem} [Read Only] \u2014 PCB Editor"):
                return False
        elif "px of window #" in line and "(PCB Editor:" not in line:
            return False
    return True


_WRAPPER_RE = re.compile(
    r"(?:\b(?:import|from)\s+(?:kicad_route|kicad_edit|kicad_lay|kipy|pcbnew)\b|(?:\bpython[0-9.]*\b|\bpy\b)[^\n]*?/tmp/[^\s\"']+\.py\b)"
)


def _is_wrapper_command(command: str, parts: list[list[str]]) -> bool:
    """Detect ad-hoc inline or /tmp Python wrappers importing KiCad router/IPC internals instead of repo scripts."""
    if any(_script_index(words, name) is not None
           for words in parts for name in ("kicad_route.py", "kicad_edit.py", "kicad_lay.py", "kicad_hover_key.py", "kicad_ipc_status.py")):
        return False
    return bool(_WRAPPER_RE.search(command))


VIEW_ONLY_KEYS = frozenset({
    "home", "pageup", "pagedown", "page_up", "page_down", "pgup", "pgdn",
    "f1", "f2", "f3", "f4", "escape", "esc", "+", "-",
})
VIEW_ONLY_MENU_ITEMS = frozenset({
    "zoom in", "zoom out", "zoom to fit", "zoom to selection", "zoom to objects", "zoom to area",
    "center", "redraw", "flip board view", "switch to next layer", "switch to previous layer",
    "high contrast mode",
})


def _is_view_only_mac(after: list[str]) -> bool:
    """True only for strictly read-only mac.py viewport navigation (Zoom/Fit/Home/PageUp/PageDown)."""
    sub = after[:1]
    if sub == ["keys"]:
        return len(after) >= 3 and all(combo.lower() in VIEW_ONLY_KEYS for combo in after[2:])
    if sub == ["menu"]:
        return (
            len(after) >= 4
            and after[2].lower() == "view"
            and (after[3].lower().startswith("zoom") or after[3].lower() in VIEW_ONLY_MENU_ITEMS)
        )
    return False


def audit_commands(entries, board_stem: str | None = None):
    """Audit extracted (command, output, exit_code, item_status[, duration_s]) shell executions across routing providers."""
    route, edit, lay, other_by_cmd = collections.Counter(), collections.Counter(), collections.Counter(), collections.Counter()
    repeated = collections.Counter()
    looks = mac_calls = other = wrapper_calls = ipc_copper_calls = gui_copper_calls = help_calls = 0
    tool_seconds = refusal_seconds = 0.0
    refusal_reasons: dict[str, dict] = {}
    edited = looked_after_edit = False
    for raw_entry in entries:
        command, output, exit_code, item_status = raw_entry[:4]
        dur = max(0.0, float(raw_entry[4] or 0.0)) if len(raw_entry) >= 5 else 0.0
        command = str(command or "").strip()
        if not command:
            continue
        repeated[command] += 1
        tool_seconds += dur
        output = str(output or "")
        item_status = str(item_status or "")
        parts = _command_parts(command)
        matched = False
        cmd_refusal_keys: list[str] = []
        parsed_statuses = _parse_tool_statuses(output) if output else []
        used_lay: set[int] = set()
        for words in parts:
            if _script_index(words, "kicad_route.py") is not None:
                matched = True
                if output:
                    status, reason = _tool_status_and_reason(output, "kicad_route.py")
                else:
                    status, reason = "no_result", ""
                route[status] += 1
                if status == "usage":
                    help_calls += 1
                else:
                    gui_copper_calls += 1
                    edited, looked_after_edit = True, False
                    if status != "closed" and status != "no_result":
                        key = f"kicad_route: {status}: {reason}" if reason else f"kicad_route: {status}"
                        cmd_refusal_keys.append(key)
            elif (idx := _script_index(words, "kicad_lay.py")) is not None:
                matched = True
                after = words[idx + 1:]
                if "-h" in after or "--help" in after:
                    verb, status, reason = "none", "usage", ""
                else:
                    verb = _edit_verb(after)
                    status, reason = (
                        _consume_lay_status(parsed_statuses, used_lay, verb, output, bool(cmd_refusal_keys))
                        if output else ("no_result", "")
                    )
                lay[status] += 1
                if status == "usage":
                    help_calls += 1
                elif status != "skipped" and verb not in ("pads", "zones", "none"):
                    ipc_copper_calls += 1
                    if status in ("laid", "deleted", "no_result"):
                        edited, looked_after_edit = True, False
                if status not in ("laid", "deleted", "done", "ok", "usage", "no_result", "skipped"):
                    key = f"kicad_lay: {status}: {reason}" if reason else f"kicad_lay: {status}"
                    cmd_refusal_keys.append(key)
            elif (idx := _script_index(words, "kicad_edit.py")) is not None:
                matched = True
                verb = _edit_verb(words[idx + 1:])
                edit[verb] += 1
                if verb not in ("list", "none"):
                    if output:
                        st, reason = _tool_status_and_reason(
                            output, "kicad_edit.py",
                            ignore_verbs=("pads", "zones", "route", "via", "pour", "batch", "delete-zones"),
                        )
                    else:
                        st, reason = "no_result", ""
                    has_fail_flag = bool(re.search(r'"ok"\s*:\s*false\b', output))
                    if has_fail_flag or st not in ("done", "ok", "moved", "deleted", "no_status", "no_result"):
                        if st not in ("usage", "no_status", "no_result"):
                            cmd_refusal_keys.append(f"kicad_edit: {st}: {reason}" if reason else f"kicad_edit: {st}")
                    else:
                        edited, looked_after_edit = True, False
            elif (idx := _script_index(words, "mac.py")) is not None:
                matched = True
                after = words[idx + 1:]
                if after[:1] == ["look"]:
                    if _valid_editor_look(after, output, exit_code, item_status, board_stem=board_stem):
                        looks += 1
                        if edited:
                            looked_after_edit = True
                else:
                    mac_calls += 1
                    if after[:1] in (["keys"], ["type"], ["mouse"], ["menu"]) and not _is_view_only_mac(after):
                        gui_copper_calls += 1
                        edited, looked_after_edit = True, False
            elif _script_index(words, "kicad_hover_key.py") is not None:
                matched = True
                gui_copper_calls += 1
                mac_calls += 1
                edited, looked_after_edit = True, False
            elif _script_index(words, "kicad_ipc_status.py") is not None:
                matched = True
                mac_calls += 1
        if cmd_refusal_keys:
            refusal_seconds += dur
            each_dur = dur / len(cmd_refusal_keys)
            for key in cmd_refusal_keys:
                bucket = refusal_reasons.setdefault(key, {"count": 0, "seconds": 0.0})
                bucket["count"] += 1
                bucket["seconds"] += each_dur
        if _is_wrapper_command(command, parts):
            wrapper_calls += 1
        if not matched:
            other += 1
            other_by_cmd[other_command_head(command, parts)] += 1
    reasons_out = {
        k: {"count": v["count"], "seconds": round(v["seconds"], 2)}
        for k, v in sorted(refusal_reasons.items(), key=lambda kv: (-kv[1]["count"], kv[0]))
    }
    return {"route": sum(route.values()), "route_closed": route["closed"],
            "route_refused": sum(n for status, n in route.items() if status not in ("closed", "usage", "no_result")),
            "route_by_status": dict(sorted(route.items())),
            "lay": sum(lay.values()), "lay_laid": lay["laid"] + lay["deleted"],
            "lay_refused": sum(n for status, n in lay.items() if status not in ("laid", "deleted", "done", "ok", "usage", "no_result", "skipped")),
            "lay_by_status": dict(sorted(lay.items())),
            "ipc_copper_calls": ipc_copper_calls, "gui_copper_calls": gui_copper_calls,
            "edit": sum(edit.values()), "edit_by_verb": dict(sorted(edit.items())),
            "look": looks, "mac": mac_calls, "other": other,
            "other_by_cmd": dict(sorted(other_by_cmd.items())),
            "looked_after_edit": looked_after_edit, "wrapper_calls": wrapper_calls,
            "repeats": {key: count for key, count in repeated.items() if count > 1},
            "tool_seconds": round(tool_seconds, 2),
            "help_calls": help_calls,
            "refusal_seconds": round(refusal_seconds, 2),
            "refusal_reasons": reasons_out}


def calls(log, board_stem: str | None = None):
    def _entries():
        for event in _lines(log):
            if event.get("type") != "item.completed":
                continue
            item = event.get("item") or {}
            if item.get("type") != "command_execution":
                continue
            yield (
                item.get("command") or "",
                item.get("aggregated_output") or item.get("output") or "",
                item.get("exit_code"),
                item.get("status") or "",
            )

    return audit_commands(_entries(), board_stem=board_stem)


def _record(sink, event):
    print(json.dumps(event, separators=(",", ":"), ensure_ascii=True), file=sink, flush=True)


def _event_error(event, model, operator):
    kind = event.get("type", "")
    item = event.get("item") or {}
    if "rerout" in kind.lower() or any(reported != model for reported in
            (event.get("model"), item.get("model")) if isinstance(reported, str)):
        return f"The provider changed the requested model {model}"
    if kind in ("error", "turn.failed") or item.get("type") == "error":
        detail = event.get("message") or event.get("error") or item.get("message") or kind
        return str(detail)[:500]
    if kind.startswith("item.") and item.get("type") == "mcp_tool_call":
        return "Unexpected tool in the Codex routing transcript"
    forbidden = ("file_change", "web_search", "collab_tool_call") if operator else (
        "command_execution", "file_change", "web_search", "collab_tool_call")
    if item.get("type") in forbidden:
        return f"Unexpected tool in Codex transcript: {item['type']}"
    return ""


def _compact_images(event):
    """Image bytes are already evidence in editor captures; keep the call and its status in the transcript."""
    item = event.get("item") or {}
    result = item.get("result") or {}
    if isinstance(result, dict):
        for block in result.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "image":
                block.pop("data", None)


def run(binary, prompt, model, effort, repo, env, log, seconds, *, pid=None, images=()):
    """Run one new thread. A missing, failed or unmetered turn cannot supply a routing or review result."""
    operator = pid is not None
    reason, answer, metered = "", "", False
    with pathlib.Path(log).open("a", encoding="utf-8") as sink, tempfile.TemporaryDirectory(prefix="routing-codex-") as tmp:
        _record(sink, {"type": "routing.invocation_started", "requested_model": model, "role": "operator" if operator else "review"})
        try:
            root = pathlib.Path(tmp)
            catalog = root / "models.json"
            _catalog(env, model, catalog, operator=operator)
            argv = _argv(binary, model, effort, pathlib.Path(repo), env.get("BOARD"), pid, catalog, images,
                         env.get("ROUTING_HANDOFF_AT"))
            allowed_env = OPERATOR_ENV if operator else REVIEW_ENV
            clean_env = {k: env[k] for k in allowed_env if k in env}
            work_dir = pathlib.Path(repo) if operator else root
            with (root / "stdout").open("w+b") as raw, (root / "stderr").open("w+b") as err:
                proc = subprocess.Popen(argv, cwd=work_dir, env=clean_env, stdin=subprocess.PIPE, stdout=raw,
                                        stderr=err, start_new_session=True)
                try:
                    proc.communicate(prompt.encode("utf-8"), timeout=seconds)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    proc.wait()
                    reason = ("Operator did not hand back the editor within the handoff grace period" if operator
                              else "Independent review timed out")
                raw.seek(0)
                for line in raw:
                    try:
                        event = json.loads(line)
                        if not isinstance(event, dict) or not event.get("type") or str(event["type"]).startswith("routing."):
                            raise ValueError()
                    except (UnicodeDecodeError, ValueError):
                        reason = reason or "Codex emitted invalid JSONL"
                        continue
                    _compact_images(event)
                    _record(sink, event)
                    reason = reason or _event_error(event, model, operator)
                    if event["type"] == "turn.completed":
                        metered = _usage(event) is not None
                    if event["type"] == "item.completed" and event.get("item", {}).get("type") == "agent_message":
                        answer = event["item"].get("text") or ""
                if proc.returncode and not reason:
                    err.seek(max(0, err.tell() - 512))
                    reason = f"Codex exited {proc.returncode}: {err.read().decode('utf-8','replace').strip()}"
        except (OSError, ValueError, TypeError) as error:
            reason = str(error)
        if not metered or not answer.strip():
            reason = reason or "Codex did not return a completed turn with token usage and a final answer"
        _record(sink, {"type": "routing.invocation_finished", "ok": not reason, "metered": metered, "error": reason})
    return not reason, reason or answer
