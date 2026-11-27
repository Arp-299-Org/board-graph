#!/usr/bin/env python3
"""Steps: in each an agent changes a board in the real PCB Editor on the Mac, and the editor is the only way in.

  .venv-kicad-ipc/bin/python scripts/editor_step.py BOARD --task "Route the net /X." [--minutes 6] [--model M] [--effort E] [--review-model M] [--once]
  .venv-kicad-ipc/bin/python scripts/editor_step.py BOARD --tasks FOLDER [the same options]

Run on the Mac, on a throwaway copy, with no PCB Editor running there: the editor has one user at a time, so the
script refuses to start beside one. It knows its editor by the board's full path. --task is a run of one step; --tasks runs one step per text file in FOLDER, in
name order, all in ONE editor, opened at the start and closed at the end. --minutes sets the operator handoff
time and the time available to a separate read-only review.
For the Claude provider, the agent defaults to sonnet unless --model names another.

The script opens the editor on BOARD, refills the pours, saves, and runs the design check and audits. Each step then
locks the file and starts the agent. When the agent stops, the script refills and saves what the editor holds and
decides from that. If the checks fail with time left, the agent is sent back, so stopping is no way out; --once turns
that off. The agent's last message counts for nothing, and a timeout is a failure. A kept step's end checks are the
next step's start: the same saved file is not checked twice.

A step is kept when the script passed and the reviews left nothing open. One that is not is undone: the file is put
back from the copy taken before it, the open editor reloads it (IPC revert, its copper read back and compared with the
file, or closed and reopened if that does not match).

At the handoff time the operator finishes its gesture and returns, with 60 seconds of grace. A named connection
with only new route-quality warnings is archived for a lead decision before rollback. A round that changed no copper
ends retries. The run ends at the first step not kept, before a step when the Mac's screen is locked
or the editor is not idle and clean (closed, busy with an armed tool or a dialog, another window of it open, unsaved
edits, or another board), and at the end of the task list.

Three layers look at the work. The script decides: it fails if the file changed while locked, the editor closed or
holds another file, the design check fails, or route quality or the electrical audit has a finding the starting board
did not. The design check passes when open connections went down or findings went down with neither errors nor
non-isolated warnings going up (or when copper pours changed with no new errors or non-isolated warnings, or when
audit findings decreased cleanly without adding DRC findings). The agent is told to look at its own route before stopping.
Every round the script passes, a different, read-only agent reviews pictures and a table of what moved, and what it
leaves open goes back to the agent, at most twice; so the last word on a passing step is a review's, at most three in
all. A review after the first is given what the one before left open, word for word, and answers each: one it does not
call fixed stays open, silence included. What is open at the end, those earlier findings still open and the last
review's own MUST FIX, goes in the result as review_open, and a later round that fails the script does not clear it.
A round that saves the same board the last review saw gets no review: its findings stay open, the step is not kept, and
review_skipped says so.

Prints one JSON line per step as it ends, each step in its own step-... folder beside BOARD, then a summary line: why
the run stopped, how many were kept, and the steps' tokens and route calls added up. A step's line has its task, kept,
tokens (per round and per model, the agent's and each review's), tokens_total (each kind summed over them all), and
calls: what the agent ran, from its transcript, with the route calls by the status the route tool returned, the edit
calls by verb, the looks, the other commands, and the commands run more than once word for word. Exits 0 only when
every task was kept.

--agent replaces the agent with a shell command for stand-in checks: $TASK holds the task's text.
--provider codex / gemini runs a managed operator with read-only review.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
import zipfile

import tool_paths

sys.modules.setdefault("editor_step", sys.modules[__name__])

if __name__ == "__main__":
    tool_paths.take_argument()

import board_context  # noqa: E402
import validate_board  # noqa: E402
from editor_step_audit import (  # noqa: E402
    CODEX_MODEL,
    GEMINI_MODEL,
    GEMINI_REVIEW_MODEL,
    MANAGED_PROVIDERS,
    NO_AGENT,
    ROUTING_MODEL,
    TOKEN_KINDS,
    _board_zones_summary,
    _effective_review_model,
    _effective_review_provider,
    _extract_copper_records,
    _step_timing,
    audits,
    calls,
    changed_copper_details,
    check_passes,
    copper,
    decide,
    design_check,
    finding_details,
    known_total,
    moved_table,
    new_findings,
    route_status,
    run_totals,
    step_evidence,
    tokens,
    tokens_total,
)
from editor_step_kicad import (  # noqa: E402
    _archive_partial_candidate,
    _write_status,
    board_path,
    close_editor,
    editor_board,
    editor_matches,
    expected_connection,
    expected_connections,
    file_copper,
    final_connections,
    final_pours,
    held_board,
    is_locked,
    layer_pictures,
    lock,
    not_idle,
    picture_of_editor,
    put_back,
    refuse_if_editor_running,
    running_editors,
    same_board,
    save_candidate,
    saved_connection,
    screen_locked,
    sha,
)
from editor_step_kicad import open_editor as _kicad_open_editor  # noqa: E402
from editor_step_prompts import (  # noqa: E402
    BACK,
    CODEX_PROMPT,
    EARLIER,
    GEMINI_PROMPT,
    LOOK_ONLY_RETRY_PROMPT,
    PROMPT,
    RECIPE_TAIL,
    REVIEW,
    REVIEW_SEND_BACKS,
    ROUTE_QUALITY_SUMMARY,
    _doc_file,
    _ipc_cheat_sheet,
    _mac_prompt_lines,
    _routing_doc_paths,
    allowed_tools,
    format_operator_prompt,
    mode_guidance,
    operator_docs_text,
    recipe_text,
    review_reading,
)
from routing_evidence import (  # noqa: E402
    DEFAULT_ROUTING_MODE,
    NO_ANSWER,
    effective_task_mode,
    parse_task_connections,
    parse_task_mode,
    parse_task_pad_exits,
    parse_task_pours,
    review_findings,
    route_quality_warning_detail,
    saved_pours,
    step_connections,
    still_open,
)

TOOL_ROOT = tool_paths.TOOL_ROOT
SCRIPTS_PREFIX = str(TOOL_ROOT / "scripts") if tool_paths.is_external() else "scripts"

sys.path.insert(0, str(TOOL_ROOT / "tools" / "kicad"))
from kicad_install import KICAD_CLI, KICAD_PCBNEW as PCBNEW  # noqa: E402

HANDOFF_GRACE_SECONDS = 60
MAX_PROGRESS_EXTENSIONS = 3


CLAUDE = str(pathlib.Path.home() / ".local/bin/claude")
PYTHON = sys.executable
ROUTING = validate_board.DESCRIPTION.routing_agent
RECIPE = validate_board.DESCRIPTION.root / ROUTING["recipe"] if ROUTING.get("recipe") else None
FOLDER = validate_board.DESCRIPTION.rel("folder")
DENIED = ["Write", "Edit", "NotebookEdit", "Agent", "WebFetch", "WebSearch", f"Read(./{FOLDER}/**)", f"Grep(./{FOLDER}/**)", f"Glob(./{FOLDER}/**)"]
ALLOWED = ["Read", "Glob", "Grep", "Skill", "Bash(date:*)",
           f"Bash({PYTHON} {SCRIPTS_PREFIX}/kicad_route.py:*)",
           f"Bash({PYTHON} {SCRIPTS_PREFIX}/kicad_edit.py:*)",
           *(f"Bash({PYTHON} {SCRIPTS_PREFIX}/mac.py {command}:*)"
             for command in ("look", "windows", "menus", "menu", "keys", "type", "mouse"))]


def open_editor(path: pathlib.Path) -> tuple:
    return _kicad_open_editor(path)


def next_move(result: dict, reviewing: bool, open_: list | None, sent_back: int, unchanged: bool = False,
              warning_candidate: bool = False) -> str:
    """After a round: 'review', 'back', or 'stop'."""
    if not result["passed"] and not warning_candidate:
        return "back" if result["why"] in (
            "check_failed", "audit_failed", "pad_exit_failed", "connection_open",
            "pour_missing", "editor_busy", "missing_visual_look",
        ) else "stop"
    if not reviewing:
        return "stop"
    if open_ is None:
        return "stop" if unchanged else "review"
    return "back" if open_ and sent_back < REVIEW_SEND_BACKS else "stop"


def effort_args(effort: str | None) -> list:
    return ["--effort", effort] if effort else []


def agent_argv(prompt: str, model: str, effort: str | None, session: str, resume: bool = False, allowed: list | None = None) -> list:
    return [CLAUDE, "-p", prompt, "--model", model, *effort_args(effort), "--output-format", "stream-json", "--verbose",
            "--setting-sources", "project", "--strict-mcp-config", "--resume" if resume else "--session-id", session,
            "--allowedTools", *(allowed if allowed is not None else ALLOWED), "--disallowedTools", *DENIED]


def _append_warning_review_context(prompt: str, board: pathlib.Path, pictures: list, log: pathlib.Path,
                                   warnings, candidate, active_review_provider: str) -> str:
    archive = log.parent / candidate["archive"]
    if sha(archive) != candidate["sha256"]:
        raise ValueError("Warning candidate archive changed before independent review")
    with zipfile.ZipFile(archive) as bundle:
        saved = bundle.read("candidate.kicad_pcb")
        if hashlib.sha256(saved).hexdigest() != candidate["board_sha256"] or saved != board.read_bytes():
            raise ValueError("Saved PCB differs from the archived warning candidate")
        after_count = len(pictures) // 2
        if any(bundle.read(path.name) != path.read_bytes() for path in pictures[after_count:]):
            raise ValueError("Review capture differs from the archived warning candidate")
    prompt += "\n\nThe design check passed. The route-quality audit reported the following warnings on this saved PCB:\n"
    prompt += "\n".join(f"Warning {n}: {warning}" for n, warning in enumerate(warnings, 1))
    prompt += (f"\nPCB SHA-256: {candidate['board_sha256']}. Inspect each warning on the pictured copper. "
               "For each number write exactly one line starting FINE AS IS: Warning N: or MUST FIX: Warning N: "
               "followed by a specific geometric reason. Report any other defect normally. A bare NOTHING "
               "cannot clear a warning. Put all observations on the requested verdict or PAD lines.")
    if active_review_provider in MANAGED_PROVIDERS:
        rules = board.with_suffix(".kicad_dru")
        if rules.is_file():
            prompt += "\n\nProject routing rules:\n" + rules.read_text(encoding="utf-8")
        prompt += "\n\nExact archived KiCad PCB:\n" + saved.decode("utf-8")
    else:
        prompt += f"\nRead the exact saved PCB at {board} to inspect the geometry."
    return prompt


def _gemini_review_prompt(prompt: str, board: pathlib.Path, repo: pathlib.Path, pictures: list) -> tuple[str, list]:
    resolved_pictures = [str(pathlib.Path(p).resolve(strict=True)) for p in pictures]
    rules = board.with_suffix(".kicad_dru")
    extra_reads = [board.resolve(strict=True)]
    if rules.is_file():
        extra_reads.append(rules.resolve(strict=True))
    plan_rel = ROUTING.get("plan")
    if plan_rel:
        plan_file = _doc_file(repo, plan_rel)
        if plan_file.is_file():
            resolved_plan = plan_file.resolve(strict=True)
            if resolved_plan not in extra_reads:
                extra_reads.append(resolved_plan)
    prompt += "\n\nInspect every listed PNG picture using read_file before writing your verdict:\n"
    prompt += "\n".join(f"- {p}" for p in resolved_pictures)
    prompt += "\nRead-only file access is limited to those PNGs and:\n"
    prompt += "\n".join(f"- {p}" for p in extra_reads)
    prompt += (
        "\nDo not call read_file on any other path. Do not read AGENTS.md or other repository files; "
        "all required rules are already in this prompt. "
        "Shell, command (`run_command`), search, write, edit, web, browser, and MCP tools are disabled in review mode; "
        "use ONLY `read_file` on the listed PNGs and allowed paths."
    )
    return prompt, extra_reads


def review(board: pathlib.Path, task: str, pictures: list, table: str, earlier: list, model: str, effort: str | None, repo: pathlib.Path,
           env: dict, log: pathlib.Path, seconds: float, codex_binary=None, pad_exits=(), warnings=(), candidate=None,
           review_provider=None, gemini_binary=None) -> tuple:
    """What a different, read-only agent leaves open, and its whole answer."""
    active_review_provider = review_provider or ("codex" if codex_binary else "gemini" if gemini_binary else "claude")
    told = EARLIER.format(findings="\n".join(f"{n}. {finding}" for n, finding in enumerate(earlier, 1))) if earlier else ""
    electrical = ("A separate electrical audit was requested. Its findings are compared to the starting board."
                  if validate_board.ELECTRICAL else "This board has no separate electrical audit configured.")
    prompt = REVIEW.format(board=board, task=task, pictures=", ".join(str(p) for p in pictures), table=table, earlier=told,
                           electrical=electrical, **review_reading())
    if len(pictures) < 4 or len(pictures) % 2 != 0:
        raise ValueError("Review needs matching front, back and any inner layer views before and after the edit")
    if pad_exits:
        prompt += "\n\nInspect the saved-copper close-up for each pad. Each image is rotated with the facing short edge at the top. "
        prompt += "Cyan marks the pad axis and edge, gold the measured crossing, and cream the trace centerline.\n"
        for entry in pad_exits:
            crossing, = entry["crossings"]
            prompt += (f'{entry["pad"]}: {entry["picture"]}, offset {crossing["offset_mm"]:.4f} mm, '
                       f'{crossing["angle_deg"]:.2f} degrees from perpendicular.\n')
            pictures = [*pictures, log.parent / entry["picture"]]
        prompt += ("In addition to the verdict, write one line per pad: PAD <ref.pad>: "
                   "describe where the track meets the edge and where it first bends. Flag poor entries as MUST FIX.")
    for path in pictures:
        with path.open("rb") as image:
            if image.read(8) != b"\x89PNG\r\n\x1a\n":
                raise ValueError(f"missing or invalid review capture: {path.name}")
    if warnings:
        prompt = _append_warning_review_context(prompt, board, pictures, log, warnings, candidate, active_review_provider)
    if active_review_provider == "codex":
        import routing_codex
        for name in _routing_doc_paths():
            doc_file = _doc_file(repo, name)
            if doc_file.is_file() or name in ROUTING.get("docs", []):
                prompt += f"\n\n{name}\n{doc_file.read_text(encoding='utf-8')}"
        ok, answer = routing_codex.run(codex_binary, prompt, model, effort, repo, env, log, seconds, images=pictures)
        if not ok:
            raise ValueError(f"Codex review failed: {answer}")
        return review_findings(earlier, answer, [p["pad"] for p in pad_exits], warnings), answer
    if active_review_provider == "gemini":
        import routing_gemini
        for name in _routing_doc_paths():
            doc_file = _doc_file(repo, name)
            if doc_file.is_file() or name in ROUTING.get("docs", []):
                prompt += f"\n\n{name}\n{doc_file.read_text(encoding='utf-8')}"
        prompt, extra_reads = _gemini_review_prompt(prompt, board, repo, pictures)
        ok, answer = routing_gemini.run(
            gemini_binary, prompt, model, effort, repo, env, log, seconds,
            images=pictures, read_paths=extra_reads,
        )
        if not ok:
            raise ValueError(f"Gemini review failed: {answer}")
        return review_findings(earlier, answer, [p["pad"] for p in pad_exits], warnings), answer
    argv = [CLAUDE, "-p", prompt, "--model", model, *effort_args(effort), "--output-format", "stream-json", "--verbose",
            "--setting-sources", "project", "--strict-mcp-config", "--no-session-persistence",
            "--allowedTools", "Read", "Glob", "Grep", "--disallowedTools", "Bash", *DENIED]
    if not run_agent(argv, env, repo, log, seconds):
        raise ValueError("Reviewer failed or timed out")
    answer = ""
    for line in log.read_text(errors="replace").splitlines():
        if '"type":"result"' in line:
            message = json.loads(line)
            if message.get("is_error"):
                raise ValueError("Reviewer failed: " + str(message.get("result") or "")[:300])
            answer = str(message.get("result") or "")
    return review_findings(earlier, answer, [p["pad"] for p in pad_exits], warnings), answer


def run_agent(argv: list, env: dict, cwd: pathlib.Path, stdout: pathlib.Path, seconds: float) -> bool:
    """False if the clock ran out."""
    with stdout.open("ab") as sink, (stdout.parent / "agent-stderr.log").open("ab") as errors:
        proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=sink, stderr=errors, start_new_session=True)
        try:
            return proc.wait(timeout=seconds) == 0
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            return False


def step_folder(parent: pathlib.Path) -> pathlib.Path:
    name = f"step-{time.strftime('%m%d-%H%M%S')}"
    folder, n = parent / name, 1
    while folder.exists():
        n += 1
        folder = parent / f"{name}-{n}"
    folder.mkdir()
    return folder


def step(args, task: str, path: pathlib.Path, proc, socket: str, start_check: dict, start_audits: list, folder: pathlib.Path,
         repo: pathlib.Path) -> tuple:
    """One task in the open editor: lock, agent rounds, save, decide, review."""
    done = threading.Event()
    review_provider = _effective_review_provider(args)
    review_model = _effective_review_model(args)
    try:
        return step_rounds(args, task, path, proc, socket, start_check, start_audits, folder, repo, done)
    except Exception as error:
        try:
            mode = effective_task_mode(task)
        except ValueError:
            mode = None
        transcript = folder / "transcript.jsonl"
        review_logs = sorted(folder.glob("review*.jsonl"))
        used, ran = step_evidence(args, transcript, review_logs, board_stem=path.stem)
        return {"passed": False, "why": "step_failed", "detail": str(error), "before": start_check,
                "routing_mode": mode, "board": str(path), "pid": proc.pid, "folder": str(folder), "provider": args.provider,
                "review_provider": review_provider if review_logs else None,
                "model": args.model, "review_model": review_model, "effort": args.effort,
                "review_open": [], "reviews": len(review_logs), "review_skipped": "",
                "timing": _step_timing(args, transcript, used, ran, 0.0, 0.0, 0.0),
                "tokens": used, "tokens_total": known_total(used), "calls": ran}, None
    finally:
        done.set()
        if is_locked(path):
            lock(path, False)


def batch_seconds(requested, now=None):
    """Cap model time by the Routing card's deadline, including time already spent waiting for the editor."""
    limit = os.environ.get("BOARD_GRAPH_ROUTE_DEADLINE")
    if limit is None:
        return requested
    return min(requested, max(0, int(limit) - (time.time() if now is None else now)))


def _prepare_step_inputs(args, task: str, path: pathlib.Path, proc, socket: str, folder: pathlib.Path, repo: pathlib.Path, done):
    """Parse task contract, capture starting views, build prompts, lock board, and start tamper watcher."""
    start_text = path.read_text(encoding="utf-8")
    mode = effective_task_mode(task)
    targets = expected_connections(task)
    centered = parse_task_pad_exits(task, targets)
    pours = parse_task_pours(task)
    if centered:
        if len(targets) == 1:
            task += ("\nAt both pads cross the middle of the short edge facing the other pad, perpendicular to its edge. "
                     "Start and finish at the pad centers. Keep each leg straight through the pad and put bends in open space. "
                     "The saved check allows 0.02 mm lateral offset and 1 degree from perpendicular.")
        else:
            task += ("\nFor every listed connection, at both pads cross the middle of the short edge facing the other pad, "
                     "perpendicular to its edge. Start and finish at the pad centers. Keep each leg straight through the pad "
                     "and put bends in open space. The saved check allows 0.02 mm lateral offset and 1 degree from perpendicular.")
    if ROUTING.get("tasks") and not targets and not pours and not task.lstrip().startswith("Repair:"):
        raise ValueError("Start a task with Connection: or Pour:, or label a geometry repair with Repair:")
    already_connected = []
    for idx, target in enumerate(targets, 1):
        start_report = folder / ("connection-start.json" if len(targets) == 1 else f"connection-start-{idx}.json")
        if saved_connection(path, target, start_report):
            already_connected.append(" ".join(target))
    if already_connected:
        raise ValueError(f"The requested pads were already connected before this task: {'; '.join(already_connected)}")
    extra_layers = tuple(dict.fromkeys(layer for pour in pours for layer in pour["layers"] if layer not in ("F.Cu", "B.Cu")))
    pic_kwargs = {"extra_layers": extra_layers} if extra_layers else {}
    start_pictures = layer_pictures(socket, proc.pid, folder, "start", path.stem, **pic_kwargs)
    start_zones = _board_zones_summary(start_text) if pours else None
    expected = [sha(path)]
    lock(path, True)

    watched: list = []
    paused = threading.Event()

    def watch() -> None:
        while not done.wait(1):
            if not paused.is_set() and not watched and sha(path) != expected[0]:
                watched.append(f"{time.strftime('%H:%M:%S')}: the file changed while locked")

    threading.Thread(target=watch, daemon=True).start()

    started = time.time()
    deadline = started + args.minutes * 60
    clock = lambda at: time.strftime("%H:%M:%S", time.localtime(at))  # noqa: E731
    env = dict(os.environ, BOARD=str(path), SOCKET=socket, KICAD_API_SOCKET=socket, TASK=task, ROUTING_HANDOFF_AT=str(deadline))
    session = str(uuid.uuid4())
    transcript = folder / "transcript.jsonl"
    ctx_file = folder / "board-context.txt"
    inline_ctx, full_ctx = board_context.render_board_context(path, targets, centered=centered, board_text=start_text, full_path=ctx_file)
    if full_ctx:
        ctx_file.write_text(full_ctx, encoding="utf-8")
    op_docs = operator_docs_text(repo)
    ctx_parts = [part for part in (inline_ctx, op_docs) if part]
    board_ctx_block = ("\n\n" + "\n\n".join(ctx_parts)) if ctx_parts else ""
    copper_tools = mode_guidance(mode, PYTHON, SCRIPTS_PREFIX, proc.pid, centered=centered)
    mac_gui_line, mac_legacy_line = _mac_prompt_lines(mode, PYTHON, SCRIPTS_PREFIX)
    prompt, argv, round_prompt = "", None, ""
    if args.agent:
        argv = ["/bin/sh", "-c", args.agent]
    else:
        prompt = format_operator_prompt(
            args.provider, board=path, pid=proc.pid, python=PYTHON, scripts_prefix=SCRIPTS_PREFIX,
            copper_tools=copper_tools, mac_gui_line=mac_gui_line, mac_legacy_line=mac_legacy_line,
            task=task, now_str=clock(started), deadline_str=clock(deadline), minutes=int(args.minutes),
            board_ctx_block=board_ctx_block, start_pictures=start_pictures, recipe_str=recipe_text(PYTHON),
        )
        (folder / "prompt.md").write_text(prompt, encoding="utf-8")
        if args.provider in MANAGED_PROVIDERS:
            round_prompt = prompt
        else:
            argv = agent_argv(prompt, args.model, args.effort, session, allowed=allowed_tools(mode))
    return {
        "task": task, "start_text": start_text, "start_sha": sha(path), "mode": mode, "targets": targets, "centered": centered, "pours": pours,
        "extra_layers": extra_layers, "start_pictures": start_pictures, "start_zones": start_zones,
        "expected": expected, "watched": watched, "paused": paused, "started": started, "deadline": deadline,
        "clock": clock, "env": env, "session": session, "transcript": transcript,
        "prompt": prompt, "round_prompt": round_prompt, "argv": argv,
    }


def _invoke_operator_round(args, state: dict, round_num: int, path: pathlib.Path, proc, repo: pathlib.Path,
                           operator_images: list, operator_seconds: int) -> tuple[bool, str, float]:
    """Run one operator round across Codex, Gemini, Claude, or stand-in --agent."""
    op_started = time.monotonic()
    if args.provider == "codex":
        import routing_codex
        _write_status(f"round {round_num} operator (codex/{args.model or CODEX_MODEL})")
        finished, detail = routing_codex.run(args.codex_binary, state["round_prompt"], args.model, args.effort, repo,
                                             state["env"], state["transcript"], operator_seconds, pid=proc.pid, images=operator_images)
    elif args.provider == "gemini":
        import routing_gemini
        _write_status(f"round {round_num} operator (gemini/{args.model or GEMINI_MODEL})")
        finished, detail = routing_gemini.run(args.gemini_binary, state["round_prompt"], args.model, args.effort, repo,
                                              state["env"], state["transcript"], operator_seconds, pid=proc.pid, images=operator_images)
    else:
        _write_status(f"round {round_num} operator ({args.provider}/{args.model or ROUTING_MODEL})")
        finished = run_agent(state["argv"], state["env"], repo, state["transcript"], max(operator_seconds, 1))
        detail = ""
    elapsed = max(0.0, time.monotonic() - op_started)
    state["paused"].set()
    if args.provider in MANAGED_PROVIDERS and not state["watched"] and sha(path) != state["expected"][0]:
        state["watched"].append("the board file changed during the locked operator round")
    return finished, detail, elapsed


def _check_round_targets_and_pours(path: pathlib.Path, folder: pathlib.Path, round_num: int, state: dict, result: dict) -> None:
    """Verify named connections, centered pad exits, and declared copper pours on the saved PCB."""
    targets, centered, pours = state["targets"], state["centered"], state["pours"]
    if not (targets or pours) or "after" not in result:
        return
    saved_bytes = path.read_bytes()
    saved_sha = hashlib.sha256(saved_bytes).hexdigest()
    saved_text = saved_bytes.decode("utf-8")
    if targets:
        checked_connections = []
        merged_pads: dict[str, dict] = {}
        for idx, target in enumerate(targets, 1):
            report = folder / (f"connection-round{round_num}.json" if len(targets) == 1 else f"connection-round{round_num}-{idx}.json")
            joined = saved_connection(path, target, report, centered)
            checked_connections.append({"from": target[0], "to": target[1], "net": target[2], "before": False, "after": joined, "saved_sha256": saved_sha})
            if centered:
                entries = json.loads(report.read_text(encoding="utf-8"))["pad_exits"]
                if [entry["pad"] for entry in entries] != target[:2]:
                    raise ValueError("Saved pad-exit report does not match the named pads")
                for entry in entries:
                    pad_name, issues = entry["pad"], list(entry.get("issues", []))
                    if pad_name not in merged_pads:
                        merged_pads[pad_name] = dict(entry, issues=issues)
                    else:
                        for issue in issues:
                            if issue not in merged_pads[pad_name]["issues"]:
                                merged_pads[pad_name]["issues"].append(issue)
        if sha(path) != saved_sha:
            raise ValueError("Saved board changed during the named-pad checks")
        if len(checked_connections) == 1:
            result["connection"] = checked_connections[0]
        else:
            result["connections"] = checked_connections
        all_joined = all(c["after"] for c in checked_connections)
        open_targets = [f"{c['from']} {c['to']} {c['net']}" for c in checked_connections if not c["after"]]
        if centered:
            result["pad_exits"] = list(merged_pads.values())
            problems = [f'{p["pad"]}: {issue}' for p in result["pad_exits"] for issue in p["issues"]]
            if problems and all_joined and (result["passed"] or result["why"] == "audit_failed"):
                details = problems + (result.get("matching_geometry") or result.get("new_findings", []))
                result.update(passed=False, why="pad_exit_failed", detail="; ".join(details))
        if open_targets and result["passed"]:
            result.update(passed=False, why="connection_open", detail="Saved board still has " + ", ".join(open_targets) + " open")
    if pours:
        checked_pours = saved_pours(saved_text, pours, saved_sha)
        result["pours"] = checked_pours
        missing = [f"{p['net']} ({','.join(p['layers'])})" for p in checked_pours if p["zone_count"] == 0 or not p["filled"]]
        if missing and result["passed"]:
            result.update(passed=False, why="pour_missing", detail="Saved board is missing filled copper pour(s): " + ", ".join(missing))


def _audit_managed_calls(args, transcript: pathlib.Path, path_stem: str, mode: str, result: dict, candidate: bool) -> tuple[bool, bool]:
    """Enforce wrapper, routing mode, editor activity, and post-edit look rules for Codex/Gemini operators."""
    if args.provider == "gemini":
        import routing_gemini
        found_calls = routing_gemini.calls(transcript, board_stem=path_stem)
        label = "Gemini"
    else:
        import routing_codex
        found_calls = routing_codex.calls(transcript, board_stem=path_stem)
        label = "Codex"
    if found_calls.get("wrapper_calls"):
        result.update(passed=False, why="unauthorized_wrapper",
                      detail=f"{label} ran an ad-hoc Python wrapper instead of invoking scripts/kicad_route.py or scripts/kicad_lay.py directly")
        return True, candidate
    if mode == "ipc" and found_calls.get("gui_copper_calls"):
        result.update(passed=False, why="mode_violation", detail="Task Mode: ipc forbids kicad_route.py and GUI routing controls")
        return True, candidate
    if mode == "interactive" and found_calls.get("ipc_copper_calls"):
        result.update(passed=False, why="mode_violation", detail="Task Mode: interactive forbids scripts/kicad_lay.py")
        return True, candidate
    legacy = found_calls.get("editor_calls") or {}
    acted = (found_calls.get("route") or found_calls.get("lay") or found_calls.get("edit")
             or found_calls.get("mac") or (legacy.get("look") and (legacy.get("key") or legacy.get("mouse"))))
    if not acted:
        result.update(passed=False, why="missing_editor_evidence", detail=f"{label} transcript has no recorded routing or editor tool command")
        return True, candidate
    looked = bool(found_calls.get("looked_after_edit") or (legacy.get("look") and (legacy.get("key") or legacy.get("mouse"))))
    if not looked:
        result.update(passed=False, why="missing_visual_look",
                      detail="Operator did not run mac.py look on the assigned PCB Editor after its last edit")
        return False, False
    return False, candidate


def _run_review_phase(args, state: dict, round_num: int, path: pathlib.Path, proc, socket: str, folder: pathlib.Path,
                      result: dict, warning_candidate: bool, warning_details: list, after_pictures: list | None,
                      review_open: list, reviews: list, warning_reviews: list) -> tuple[bool, list, float, list | None]:
    """Capture screenshots if needed, verify saved SHAs, invoke independent reviewer, and record review receipt."""
    review_provider = _effective_review_provider(args)
    review_model = _effective_review_model(args)
    reviewed = sha(path)
    pic_kwargs = {"extra_layers": state["extra_layers"]} if state["extra_layers"] else {}
    if not warning_candidate:
        after_pictures = layer_pictures(socket, proc.pid, folder, f"round{round_num}", path.stem, **pic_kwargs)
    next_operator_images = list(after_pictures)
    pictures = state["start_pictures"] + after_pictures
    review_log = folder / f"review{len(reviews) + 1}.jsonl"
    rev_elapsed = 0.0
    try:
        checked_shas = ({c.get("saved_sha256") for c in step_connections(result)}
                        | {p.get("saved_sha256") for p in (result.get("pours") or ())}) - {None}
        if (checked_shas and checked_shas != {reviewed}) or sha(path) != reviewed:
            raise ValueError("Saved board changed between the checks and the review captures")
        review_seconds = batch_seconds(args.minutes * 60)
        if review_seconds < 1:
            raise ValueError("Routing card deadline reached before independent review")
        earlier = list(review_open)
        _write_status(f"round {round_num} review {len(reviews) + 1} ({review_provider}/{review_model})")
        saved_after_text = path.read_text(encoding="utf-8")
        review_table = moved_table(state["start_text"], saved_after_text)
        copper_details = changed_copper_details(state["start_text"], saved_after_text)
        if copper_details:
            review_table += "\n\nChanged copper geometry (no zone fills):\n" + copper_details
        rev_started = time.monotonic()
        try:
            new_open, answer = review(
                path, state["task"], pictures, review_table, review_open, review_model, args.effort,
                state["repo"], state["env"], review_log, review_seconds,
                getattr(args, "codex_binary", None), result.get("pad_exits", ()),
                warning_details, result.get("candidate"),
                review_provider=review_provider, gemini_binary=getattr(args, "gemini_binary", None),
            )
        finally:
            rev_elapsed = max(0.0, time.monotonic() - rev_started)
        if sha(path) != reviewed:
            raise ValueError("Saved board changed during the independent review")
        result["reviewed_sha256"] = reviewed
        result["review_receipt"] = {"schema": "board-graph/routing-review/v1", "board_sha256": reviewed,
                                    "reviewer": review_model, "answer": answer, "earlier": earlier, "warnings": list(warning_details)}
        if warning_candidate:
            warning_reviews.append({"round": round_num, "board_sha256": reviewed, "candidate": result["candidate"],
                                    "warnings": warning_details, "answer": answer, "open_findings": list(new_open)})
            result.update(passed=not new_open, why="warning_must_fix" if new_open else "warning_accepted")
        reviews.append(answer)
        return True, new_open, rev_elapsed, next_operator_images
    except (OSError, ValueError, zipfile.BadZipFile, KeyError) as error:
        if review_log.is_file():
            reviews.append(str(error))
        result.update(passed=False, why="review_failed", detail=str(error))
        return False, review_open, rev_elapsed, next_operator_images


def _round_feedback_text(args, result: dict, start_check: dict, review_open: list) -> str:
    if review_open:
        return ("A different agent reviewed the saved PCB. Correct these open findings:\n" + "\n".join(review_open)
                + "\nFix each one, or say in your last message why you will not.")
    why = result.get("why")
    if why == "missing_visual_look":
        inspect_tool = "read_file" if args.provider == "gemini" else "view_image"
        return (f'You did not inspect the board visually after your last edit. Run `{SCRIPTS_PREFIX}/mac.py look "PCB Editor"`, '
                f'call `{inspect_tool}` on the captured PNG (and on kicad_route.py\'s picture), fix anything that looks '
                'wrong, confirm KiCad is idle, and describe what you saw before handing off.')
    if why == "editor_busy":
        return ("KiCad reported that it is busy (an armed tool or modal dialog is still open), so the runner "
                f"could not save. Check {SCRIPTS_PREFIX}/kicad_ipc_status.py or {SCRIPTS_PREFIX}/mac.py look, close any open "
                "dialog and press Escape until KiCad is idle, then hand off again.")
    if why == "pad_exit_failed":
        return "Fix the saved pad exits before handoff. " + result["detail"]
    if why == "connection_open":
        return result["detail"] + ". Route the remaining open connection(s) before handing off."
    if why == "pour_missing":
        return result["detail"] + ". Lay and refill the missing copper pour(s) cleanly over IPC before handing off."
    if why == "check_failed":
        return (f'The design check on what the editor holds: {result["after"]["open"]} open, {result["after"]["errors"]} errors, '
                f'{result["after"]["findings"]} findings in all. You started from {start_check["open"]} open, '
                f'{start_check["errors"]} errors, {start_check["findings"]} findings.')
    if why == "audit_failed":
        msg = "The design check passes, but these findings are new since you started:\n" + "\n".join(result["new_findings"])
        if result.get("matching_geometry"):
            msg += "\nMatching geometry on the saved board:\n" + "\n".join(result["matching_geometry"])
        return msg
    return ""


def _maybe_grant_extension(args, state: dict, round_num: int, start_check: dict, result: dict) -> bool:
    best_open = state.setdefault("best_open", start_check.get("open", 0))
    prog_ext = state.setdefault("progress_extensions", 0)
    look_ext = state.setdefault("look_extensions", 0)
    after = result.get("after")
    clean_progress = (
        result.get("why") == "connection_open"
        and isinstance(after, dict)
        and after.get("open", 999999) < best_open
        and after.get("errors", 1) <= start_check.get("errors", 0)
        and not result.get("new_findings")
        and not any(p.get("issues") for p in result.get("pad_exits", []))
    )
    if clean_progress:
        state["best_open"] = after["open"]
    seconds = int(state["deadline"] - time.time())
    if seconds >= 30:
        return True
    if clean_progress and prog_ext < MAX_PROGRESS_EXTENSIONS and batch_seconds(120) >= 60:
        ext = min(max(180, int(args.minutes * 60 * 0.5)), batch_seconds(360))
        state["deadline"] = time.time() + ext
        state["env"]["ROUTING_HANDOFF_AT"] = str(state["deadline"])
        state["progress_extensions"] = prog_ext + 1
        _write_status(
            f"round {round_num} clean progress ({after['open']} open left); "
            f"granting {ext}s extension ({prog_ext + 1}/{MAX_PROGRESS_EXTENSIONS})"
        )
        return True
    if result.get("why") == "missing_visual_look" and look_ext < 1 and batch_seconds(90) >= 45:
        ext = min(90, batch_seconds(90))
        state["deadline"] = time.time() + ext
        state["env"]["ROUTING_HANDOFF_AT"] = str(state["deadline"])
        state["look_extensions"] = look_ext + 1
        _write_status(f"round {round_num} missing visual look; granting {ext}s inspection extension")
        return True
    return False


def _is_timeout_detail(detail: str) -> bool:
    text = str(detail or "").lower()
    return any(tok in text for tok in ("timed out", "timeout", "handoff grace", "deadline"))


def _preserve_timeout_editor_state(path: pathlib.Path, folder: pathlib.Path, round_num: int, proc, socket: str,
                                   start_check: dict, state: dict, result: dict) -> None:
    """Save and archive DRC-clean editor copper after a timeout before step cleanup reverts the file."""
    if state.get("watched") or round_num < 1 or proc is None or (hasattr(proc, "poll") and proc.poll() is not None):
        return
    try:
        lock(path, False)
        held = editor_board(socket)
        if not same_board(held, path):
            return
        editor_board(socket, save=True, refill=True)
        saved_text = path.read_text(encoding="utf-8")
        if sha(path) == state.get("start_sha") or copper(saved_text) == copper(state["start_text"]):
            return
        after = design_check(path, folder / "drc-end.json")
        result["after"] = after
        if after.get("errors", 1) <= start_check.get("errors", 0) and (
            after.get("open", 999999) < start_check.get("open", 0) or after.get("errors", 1) == 0
        ):
            cand_conn = result.get("connections") or result.get("connection")
            result["candidate"] = _archive_partial_candidate(path, folder, f"round{round_num}", socket, proc.pid, cand_conn)
    except Exception:
        pass


def _finalize_step_result(args, state: dict, path: pathlib.Path, proc, socket: str, folder: pathlib.Path, start_check: dict,
                          result: dict, end_audits, rounds: int, reviews: list, review_open: list, review_skipped: str,
                          stalled: bool, first_check_failure, warning_reviews: list,
                          operator_wall_s: float, review_wall_s: float):
    if not result.get("passed") and "candidate" not in result:
        why = result.get("why")
        tag_round = max(1, rounds)
        if why == "timeout" or (why == "card_deadline" and "after" not in result) or (
            why == "operator_failed" and _is_timeout_detail(result.get("detail", ""))
        ):
            _preserve_timeout_editor_state(path, folder, tag_round, proc, socket, start_check, state, result)
        else:
            after = result.get("after")
            clean_after = (
                why in ("connection_open", "review_failed", "card_deadline", "missing_visual_look")
                and isinstance(after, dict)
                and after.get("errors", 1) <= start_check.get("errors", 0)
                and (
                    after.get("open", 999999) < start_check.get("open", 0)
                    or (
                        after.get("open", 999999) <= start_check.get("open", 0)
                        and after.get("errors", 1) == 0
                        and sha(path) != state.get("start_sha")
                    )
                )
            )
            if clean_after and rounds >= 1:
                try:
                    cand_conn = result.get("connections") or result.get("connection")
                    result["candidate"] = _archive_partial_candidate(path, folder, f"round{tag_round}", socket, proc.pid, cand_conn)
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                    pass
    total_s = round(time.time() - state["started"], 1)
    review_logs = [folder / f"review{n + 1}.jsonl" for n in range(len(reviews))]
    used, ran = step_evidence(args, state["transcript"], review_logs, board_stem=path.stem)
    timing = _step_timing(args, state["transcript"], used, ran, total_s, operator_wall_s, review_wall_s)
    refused_count = (int(ran.get("lay_refused") or 0) + int(ran.get("route_refused") or 0)) if isinstance(ran, dict) else 0
    status_word = "kept" if result.get("passed") else result.get("why", "done")
    refusal_reasons = (ran.get("refusal_reasons") or {}) if isinstance(ran, dict) else {}
    top_refusal = next(iter(refusal_reasons), "")
    top_label = top_refusal.split(":", 2)[1].strip() if ":" in top_refusal else top_refusal
    ref_note = f", refused {refused_count}" + (f" ({top_label})" if top_label else "") if refused_count else ""
    _write_status(f"finished ({status_word}, {timing['total_s']:.0f}s: llm {timing['llm_s']:.0f}s, "
                  f"tools {timing['tool_s']:.0f}s, rev {timing['review_s']:.0f}s, think {timing['thinking_tokens']:,}{ref_note})")
    result.setdefault("before", start_check)
    if warning_reviews:
        result["warning_reviews"] = warning_reviews
    result["first_check_failure"] = first_check_failure
    result.update(board=str(path), pid=proc.pid, routing_mode=state["mode"], seconds=total_s, rounds=rounds, reviews=len(reviews),
                  review_open=review_open, review_skipped=review_skipped, stalled=stalled, model=args.model,
                  review_model=_effective_review_model(args), provider="stand-in" if args.agent else args.provider,
                  review_provider=_effective_review_provider(args) if reviews else None, effort=args.effort, timing=timing,
                  tokens=used, tokens_total=NO_AGENT if args.agent else known_total(used),
                  calls=NO_AGENT if args.agent else ran, folder=str(folder))
    (folder / "reviews.txt").write_text("\n\n".join(reviews))
    if args.provider not in MANAGED_PROVIDERS:
        picture_of_editor(proc.pid, folder / "end.png")
    return result, end_audits


def step_rounds(args, task, path, proc, socket, start_check, start_audits, folder, repo, done):
    """Execute bounded operator -> DRC/audit -> review rounds for one routing step."""
    state = _prepare_step_inputs(args, task, path, proc, socket, folder, repo, done)
    state["repo"] = repo
    managed, reviewing = args.provider in MANAGED_PROVIDERS, not args.agent
    rounds = sent_back = busy_retries = look_retries = 0
    reviews, review_open, warning_reviews = [], [], []
    stalled, reviewed, review_skipped, first_check_failure = False, None, "", None
    operator_wall_s = review_wall_s = 0.0
    operator_images = list(state["start_pictures"])
    last_copper = dict(copper(state["start_text"])) if managed else None
    while True:
        rounds += 1
        remaining = max(0, state["deadline"] - time.time())
        operator_seconds = batch_seconds(remaining if args.agent else remaining + HANDOFF_GRACE_SECONDS)
        if operator_seconds < 1:
            result, end_audits = dict(passed=False, why="card_deadline", detail="Routing card deadline reached", before=start_check), None
            break
        finished, detail, op_s = _invoke_operator_round(args, state, rounds, path, proc, repo, operator_images, operator_seconds)
        operator_wall_s += op_s
        if managed and not finished:
            result, end_audits = {"passed": False, "why": "operator_failed", "detail": detail, "before": start_check}, None
            break
        _write_status(f"round {rounds} checking board")
        decide_kwargs = {"before_zones": state["start_zones"]} if state["pours"] else {}
        result, end_audits = decide(path, start_check, start_audits, proc, socket, state["watched"], finished, folder, f"round{rounds}", **decide_kwargs)
        _check_round_targets_and_pours(path, folder, rounds, state, result)
        if not result["passed"] and first_check_failure is None:
            first_check_failure = {k: result[k] for k in ("why", "detail", "before", "after", "new_findings",
                                                           "matching_geometry", "connection", "connections", "pours", "pad_exits") if k in result}
        step_conns, step_pours = step_connections(result), (result.get("pours") or [])
        connected = ((not step_conns or all(c.get("after") is True for c in step_conns))
                     and (not step_pours or all(p.get("filled") is True and p.get("zone_count", 0) >= 1 for p in step_pours))
                     and bool(step_conns or step_pours))
        warnings = result.get("new_findings", [])
        candidate = (connected and result["why"] == "audit_failed" and warnings
                     and all(line.startswith("route quality: warning ") for line in warnings))
        if managed and (result["passed"] or candidate):
            stop_now, candidate = _audit_managed_calls(args, state["transcript"], path.stem, state["mode"], result, candidate)
            if stop_now:
                break
        warning_candidate = bool(candidate and reviewing)
        warning_details, after_pictures = [], None
        if warning_candidate:
            try:
                candidate_conn = result["connections"] if "connections" in result else result.get("connection")
                result["candidate"], after_pictures = save_candidate(
                    path, folder, f"round{rounds}", socket, proc.pid, candidate_conn,
                    pours=result.get("pours"), extra_layers=state["extra_layers"],
                )
                warning_details = result.get("matching_geometry") or []
                if any(not any(detail == label or detail.startswith(label + ",") for detail in warning_details) for label in warnings):
                    raise ValueError("A route-quality warning is missing its measured geometry")
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                result.update(why="candidate_capture_failed", detail=str(error))
                break
        unchanged = reviewed is not None and sha(path) == reviewed
        move = next_move(result, reviewing, None, sent_back, unchanged, warning_candidate)
        if (result["passed"] or warning_candidate) and reviewing and unchanged:
            review_skipped = ("the agent saved the same board review " + str(len(reviews))
                              + " saw after it was sent back: no review ran, and that review's findings stay open")
        if move == "review":
            ok, review_open, rev_s, next_operator_images = _run_review_phase(
                args, state, rounds, path, proc, socket, folder, result,
                warning_candidate, warning_details, after_pictures, review_open, reviews, warning_reviews,
            )
            if next_operator_images is not None:
                operator_images = next_operator_images
            review_wall_s += rev_s
            if not ok:
                break
            reviewed = result["reviewed_sha256"]
            move = next_move(result, reviewing, review_open, sent_back, warning_candidate=warning_candidate)
        what = _round_feedback_text(args, result, start_check, review_open if move == "back" and result.get("review_receipt") else [])
        if move != "back" or args.agent or args.once:
            break
        if not _maybe_grant_extension(args, state, rounds, start_check, result):
            break
        if result["why"] == "editor_busy":
            busy_retries += 1
            if busy_retries > 2:
                break
        if result["why"] == "missing_visual_look":
            look_retries += 1
            if look_retries > 1:
                break
        if result["passed"] or warning_candidate:
            sent_back += 1
        now_copper = dict(copper(path.read_text(encoding="utf-8")))
        if now_copper == last_copper and not warning_candidate and result["why"] not in ("editor_busy", "missing_visual_look"):
            stalled = True
            break
        last_copper = now_copper
        state["expected"][0] = sha(path)
        lock(path, True)
        state["paused"].clear()
        seconds = max(1, int(state["deadline"] - time.time()))
        if managed:
            if result["why"] == "missing_visual_look":
                state["round_prompt"] = LOOK_ONLY_RETRY_PROMPT.format(path=path, pid=proc.pid, what=what)
            else:
                state["round_prompt"] = f"{state['prompt']}\n\nThe editor still holds the previous round's work.\n{what}"
        else:
            state["argv"] = agent_argv(
                BACK.format(what=what, now=state["clock"](time.time()), deadline=state["clock"](state["deadline"]), seconds=seconds),
                args.model, args.effort, state["session"], resume=True, allowed=allowed_tools(state["mode"]),
            )
    return _finalize_step_result(
        args, state, path, proc, socket, folder, start_check, result, end_audits, rounds, reviews,
        review_open, review_skipped, stalled, first_check_failure, warning_reviews,
        operator_wall_s, review_wall_s,
    )


def stop_after(name: str, result: dict, keep_going: bool) -> str:
    """Why the run stops after this step, or '' to go on."""
    if result["kept"] or keep_going:
        return ""
    return f"not kept: {name}, {result['why']}" + (", review left MUST FIX open" if result["passed"] else "") + \
           (f" ({result['review_skipped']})" if result["review_skipped"] else "")


def tasks_of(args) -> list:
    if args.task is not None:
        return [("task", args.task)]
    files = sorted(file for file in args.tasks.iterdir() if file.is_file())
    if not files:
        raise SystemExit(f"no task files in {args.tasks}")
    return [(file.name, file.read_text(encoding="utf-8").strip()) for file in files]


def configure_provider(args):
    args.review_provider = getattr(args, "review_provider", None) or args.provider
    args.codex_binary = None
    args.gemini_binary = None
    if (args.provider in MANAGED_PROVIDERS or args.review_provider in MANAGED_PROVIDERS) and (args.agent or args.keep_going):
        raise ValueError("managed routing providers (codex/gemini) cannot use the stand-in or continue after a failed task")
    if args.provider == "codex":
        args.model = args.model or CODEX_MODEL
        args.effort = args.effort or "low"
    elif args.provider == "gemini":
        args.model = args.model or GEMINI_MODEL
    else:
        args.model = args.model or ROUTING_MODEL
    args.review_model = _effective_review_model(args)
    if "codex" in (args.provider, args.review_provider):
        import routing_codex
        args.codex_binary = routing_codex.preflight()
    if "gemini" in (args.provider, args.review_provider):
        import routing_gemini
        args.gemini_binary = routing_gemini.preflight()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("board", type=pathlib.Path)
    given = parser.add_mutually_exclusive_group(required=True)
    given.add_argument("--task", help="the task of a run of one step")
    given.add_argument("--tasks", type=pathlib.Path, help="a folder of task text files, one step each, in name order")
    parser.add_argument("--minutes", type=float, default=6, help="operator handoff time and separate review window, per step")
    parser.add_argument("--provider", choices=["claude", "codex", "gemini"], default="claude", help="the routing operator (and default independent reviewer)")
    parser.add_argument("--review-provider", choices=["claude", "codex", "gemini"], help="the independent reviewer provider, if not --provider")
    parser.add_argument("--model", help=f"operator model (Claude: {ROUTING_MODEL}, Codex: {CODEX_MODEL}, Gemini: {GEMINI_MODEL})")
    parser.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], help="Codex defaults to low effort")
    parser.add_argument("--review-model", help=f"the reviewer's model, if not --model (or the review provider's default; Gemini: {GEMINI_REVIEW_MODEL})")
    parser.add_argument("--once", action="store_true", help="do not send the agent back")
    parser.add_argument("--agent", help="a shell command in place of the agent; $BOARD, $SOCKET and $TASK are set")
    parser.add_argument("--keep-going", action="store_true", help="go on after a step not kept (the self-test's cases share one editor)")
    args = parser.parse_args()
    if not math.isfinite(args.minutes) or args.minutes <= 0:
        parser.error("--minutes must be a positive finite number")
    try:
        configure_provider(args)
    except (ValueError, OSError) as error:
        parser.error(str(error))

    path = args.board.resolve()
    if not path.is_file():
        raise SystemExit(f"no such board: {path}")
    repo = tool_paths.project_root()
    if repo in path.parents:
        raise SystemExit("run this on a throwaway copy outside the checkout")
    tasks = tasks_of(args)
    if screen_locked():
        raise SystemExit("the Mac's screen is locked: nothing ran")
    refuse_if_editor_running()

    proc, socket = open_editor(path)
    pids = [proc.pid]
    try:
        folder = step_folder(path.parent)
        editor_board(socket, save=True, refill=True)
        start_check = design_check(path, folder / "drc-start.json")
        start_audits = audits(path, folder, "start")
        results, kept, stopped, restored = [], 0, "every task was kept", None
        for number, (name, task) in enumerate(tasks):
            if batch_seconds(1) < 1:
                stopped = "Routing card deadline reached"
                break
            if screen_locked():
                stopped = "the Mac's screen is locked"
                break
            why = not_idle(proc, socket, path)
            if why:
                stopped = f"editor not ready: {why}"
                break
            if number:
                folder = step_folder(path.parent)
            copy = folder / f"before-{path.name}"
            shutil.copyfile(path, copy)
            result, end_audits = step(args, task, path, proc, socket, start_check, start_audits, folder, repo)
            results.append(result)
            result.update(task=name, kept=bool(result["passed"] and not result["review_open"]))
            if not result["kept"]:
                restored, proc, socket = put_back(path, copy, proc, socket)
                pids.append(proc.pid)
                result["restored"] = restored
            (folder / "result.json").write_text(json.dumps(result, indent=1))
            print(json.dumps(result), flush=True)
            why = stop_after(name, result, args.keep_going)
            if why:
                stopped = why
                break
            if not result["kept"]:
                stopped = f"the end of the task list (--keep-going), {number + 1 - kept} not kept"
                continue
            kept += 1
            start_check, start_audits = result["after"], end_audits
    finally:
        close_editor(proc)
    try:
        final = final_connections(path, results, folder) if kept > 1 else None
    except (ValueError, KeyError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        final = {"problem": str(error)}
    try:
        final_p = final_pours(path, results) if kept > 1 else None
    except (ValueError, KeyError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        final_p = {"problem": str(error)}
    summary_payload = {
        "stopped": stopped, "kept": kept, "tasks": len(tasks),
        "final_connections": final,
        **({"final_pours": final_p} if final_p is not None else {}),
        **run_totals(results),
        "editor_pids": sorted(set(pids)), "restored": restored,
    }
    print(json.dumps(summary_payload), flush=True)
    return 0 if kept == len(tasks) and not (final and final.get("problem")) and not (final_p and final_p.get("problem")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
