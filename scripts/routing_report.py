#!/usr/bin/env python3
"""Run evidence formatting, profiling, note rendering, and status parsing for routing_run."""
from __future__ import annotations

import collections
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import routing_codex  # noqa: E402
import routing_evidence  # noqa: E402

STEP_FIELDS = (
    "task", "kept", "why", "routing_mode", "autoroute", "before", "after", "rounds", "reviews",
    "review_open", "seconds", "timing", "calls", "tokens_total", "provider", "model",
    "review_provider", "review_model", "detail", "connection", "connections", "pours",
    "first_check_failure", "new_findings", "matching_geometry", "candidate", "pad_exits",
    "reviewed_sha256", "warning_reviews", "review_receipt",
)
SUMMARY_FIELDS = (
    "stopped", "kept", "tasks", "tokens_total", "route_calls", "route_closed", "route_refused",
    "lay_calls", "lay_laid", "lay_refused", "autoroute_steps", "timing", "refusals",
    "final_connections", "final_pours",
)
TOKEN_KINDS = ("input", "output", "cache_read", "cache_write")


def tokens(steps: list):
    """The run's tokens, every step's agent and reviews: input, output and cache, summed. None when a step has no
    count (a stand-in agent)."""
    if not steps or any(not isinstance(step.get("tokens_total"), dict) for step in steps):
        return None
    return sum(step["tokens_total"].get(kind) or 0 for step in steps for kind in TOKEN_KINDS)


def outcome(steps: list, summary, changed: list) -> str:
    """more_work when a step was kept and its board can be adopted; needs_lead otherwise. Never pass."""
    if (not summary or changed or not steps or summary.get("tasks") != len(steps)
            or not all(step.get("kept") for step in steps)):
        return "needs_lead"
    return "more_work"


def checks_span(steps: list) -> tuple:
    """The design check before the run (the first step's start) and after it (the last kept step's end)."""
    before = next((step["before"] for step in steps if isinstance(step.get("before"), dict)), None)
    after = next((step["after"] for step in reversed(steps) if step.get("kept") and isinstance(step.get("after"), dict)),
                 before)
    return before, after


def span(before, after, key) -> str:
    return f"{before[key]} to {after[key]}" if before and after else "unknown"


def _collect_profile(steps: list, summary: dict | None) -> tuple[dict | None, dict | None]:
    """Gather passive timing breakdown and refusal friction from summary or step records (never fails or gates)."""
    timing = summary.get("timing") if isinstance(summary, dict) and isinstance(summary.get("timing"), dict) else None
    if timing is None:
        timed = [s["timing"] for s in steps if isinstance(s.get("timing"), dict)]
        if timed:
            timing = {
                "total_s": round(sum(float(t.get("total_s") or 0.0) for t in timed), 1),
                "llm_s": round(sum(float(t.get("llm_s") or 0.0) for t in timed), 1),
                "tool_s": round(sum(float(t.get("tool_s") or 0.0) for t in timed), 1),
                "review_s": round(sum(float(t.get("review_s") or 0.0) for t in timed), 1),
                "harness_s": round(sum(float(t.get("harness_s") or 0.0) for t in timed), 1),
                "refusal_s": round(sum(float(t.get("refusal_s") or 0.0) for t in timed), 1),
                "thinking_tokens": sum(int(t.get("thinking_tokens") or 0) for t in timed),
            }
    refusals = summary.get("refusals") if isinstance(summary, dict) and isinstance(summary.get("refusals"), dict) else None
    if refusals is None:
        step_calls = [s["calls"] for s in steps if isinstance(s.get("calls"), dict)]
        refusals = routing_codex.summarize_refusals(step_calls)
    return timing, refusals


def _collect_extra_tools(steps: list, summary: dict | None) -> dict | None:
    extra = summary.get("extra_tools") if isinstance(summary, dict) and isinstance(summary.get("extra_tools"), dict) else None
    if extra is not None:
        return extra
    step_calls = [s["calls"] for s in steps if isinstance(s.get("calls"), dict)]
    return routing_codex.summarize_extra_tools(step_calls)


def format_profile(steps: list, summary: dict | None) -> str:
    """Plain-English passive run profile printed on delivery and delivery notes."""
    timing, refusals = _collect_profile(steps, summary)
    extra = _collect_extra_tools(steps, summary)
    if timing is None and refusals is None and extra is None:
        return ""
    lines = []
    if timing is not None:
        lines.append(
            f"Run profile: {float(timing.get('total_s') or 0.0):.0f}s total "
            f"(LLM {float(timing.get('llm_s') or 0.0):.0f}s, "
            f"tools {float(timing.get('tool_s') or 0.0):.0f}s, "
            f"review {float(timing.get('review_s') or 0.0):.0f}s, "
            f"harness {float(timing.get('harness_s') or 0.0):.0f}s; "
            f"thinking {int(timing.get('thinking_tokens') or 0):,} tok)."
        )
    if refusals is not None:
        by_reason = refusals.get("by_reason") or {}
        refused_total = sum(int(v.get("count") or 0) for v in by_reason.values())
        refusal_s = float(refusals.get("refusal_seconds") or 0.0)
        help_calls = int(refusals.get("help_calls") or 0)
        top = [f"{k} ({int(v.get('count') or 0)}x)" for k, v in list(by_reason.items())[:3]]
        top_text = (" Top: " + "; ".join(top) + ".") if top else ""
        lines.append(
            f"Tool friction: {refused_total} refusal(s) ({refusal_s:.1f}s), {help_calls} --help call(s).{top_text}"
        )
        repeated = refusals.get("repeated_across_steps") or []
        if repeated:
            rep_text = "; ".join(
                f"{r['reason']} ({int(r.get('steps') or 0)} steps, {int(r.get('count') or 0)}x)"
                for r in repeated[:3]
            )
            lines.append(f"Repeated across steps: {rep_text}.")
    if extra is not None:
        parts = []
        edit_by_verb = extra.get("edit_by_verb") or {}
        if edit_by_verb:
            verbs = ", ".join(
                f"{k}={int(v)}" for k, v in sorted(edit_by_verb.items(), key=lambda kv: (-int(kv[1]), kv[0]))
            )
            parts.append(f"kicad_edit ({verbs})")
        other_by_cmd = extra.get("other_by_cmd") or {}
        if other_by_cmd:
            cmds = ", ".join(
                f"{k}={int(v)}" for k, v in sorted(other_by_cmd.items(), key=lambda kv: (-int(kv[1]), kv[0]))
            )
            parts.append(f"other ({cmds})")
        if parts:
            lines.append("Extra tool calls: " + "; ".join(parts) + ".")
    return "\n".join(lines)


def _note_call_lines(steps: list, summary, provider) -> list[str]:
    """Summarize editor/route call counts and solver usage for a routing note."""
    words: list[str] = []
    if provider in ("codex", "gemini") and any(
        isinstance(step.get("calls"), dict) and step["calls"].get("editor_calls") for step in steps
    ):
        controls = collections.Counter()
        for step in steps:
            if isinstance(step.get("calls"), dict):
                controls.update(step["calls"].get("editor_calls", {}))
        label = "Codex" if provider == "codex" else "Gemini"
        words.append(
            f"{label} editor calls: "
            + (", ".join(f"{name} {count}" for name, count in sorted(controls.items())) or "none recorded")
            + "."
        )
    elif summary and isinstance(summary.get("route_calls"), int):
        lay_calls = summary.get("lay_calls")
        if isinstance(lay_calls, int) and lay_calls > 0:
            parts = [f"IPC lay calls {lay_calls}: {summary.get('lay_laid', 0)} laid, {summary.get('lay_refused', 0)} refused"]
            if summary["route_calls"] > 0:
                parts.append(
                    f"route calls {summary['route_calls']}: {summary['route_closed']} closed, "
                    f"{summary['route_refused']} refused"
                )
            words.append("; ".join(parts) + ".")
        else:
            words.append(
                f"Route calls {summary['route_calls']}: {summary['route_closed']} closed, "
                f"{summary['route_refused']} refused."
            )
    else:
        words.append("Route calls: not counted (a stand-in agent).")
    autoroute_steps = [
        step for step in steps if step.get("routing_mode") == "autoroute" or isinstance(step.get("autoroute"), dict)
    ]
    if autoroute_steps:
        solvers = sorted({
            str((step.get("autoroute") or {}).get("solver") or "kicad-routing-tools")
            for step in autoroute_steps
        })
        words.append(f"Autoroute steps {len(autoroute_steps)} ({', '.join(solvers)}).")
    return words


def _note_unkept_lines(steps: list) -> list[str]:
    """Format failure/candidate diagnostics for unkept steps."""
    words: list[str] = []
    for step in steps:
        if step.get("kept"):
            continue
        first = step["calls"].get("first_editor_failure") if isinstance(step.get("calls"), dict) else None
        if first:
            words.append(f"First editor refusal: {first['tool']} {first['arguments']}: {first['reason']}.")
        check = step.get("first_check_failure") or {}
        details = check.get("matching_geometry") or check.get("new_findings") or []
        if details:
            words.append("First failed check: " + "; ".join(details[:3]) + ".")
        candidate = step.get("candidate") or {}
        if candidate.get("evidence"):
            conns = routing_evidence.step_connections(step)
            summary_text = ", ".join(f"{pair['from']} to {pair['to']} on {pair['net']}" for pair in conns)
            phrase = f"Saved candidate connects {summary_text}." if summary_text else "Saved candidate."
            words.append(
                f"{phrase} Review its board, pictures and reports in {candidate['evidence']}. "
                "This candidate was not adopted."
            )
        why = step.get("why", "no reason given")
        if step.get("detail"):
            why += f", {step['detail']}"
        if step.get("review_open"):
            why += f", the review left {len(step['review_open'])} MUST FIX open"
        words.append(f"Not kept: {step.get('task')} ({why}).")
    return words


def _note_tail_lines(steps: list, summary, changed: list, kept: int, problem: str, allow_pours: bool) -> list[str]:
    """Format timing, refusal summary, and adoption status at the end of note()."""
    words: list[str] = []
    timing_prof, refusal_prof = _collect_profile(steps, summary)
    if timing_prof is not None:
        words.append(
            f"Time: {float(timing_prof.get('total_s') or 0.0):.0f}s "
            f"(LLM {float(timing_prof.get('llm_s') or 0.0):.0f}s, "
            f"tools {float(timing_prof.get('tool_s') or 0.0):.0f}s, "
            f"review {float(timing_prof.get('review_s') or 0.0):.0f}s; "
            f"thinking {int(timing_prof.get('thinking_tokens') or 0):,})."
        )
    if refusal_prof is not None:
        by_reason = refusal_prof.get("by_reason") or {}
        if by_reason:
            top_k, top_v = next(iter(by_reason.items()))
            extra_reasons = f", +{len(by_reason) - 1} other" if len(by_reason) > 1 else ""
            words.append(f"Refusals: {top_k} ({int(top_v.get('count') or 0)}x{extra_reasons}).")
        help_calls = int(refusal_prof.get("help_calls") or 0)
        if help_calls:
            words.append(f"Help calls: {help_calls}.")
        repeated = refusal_prof.get("repeated_across_steps") or []
        if repeated:
            rep_text = "; ".join(
                f"{r['reason']} ({int(r.get('steps') or 0)} steps, {int(r.get('count') or 0)}x)"
                for r in repeated[:3]
            )
            words.append(f"Repeated across steps: {rep_text}.")
    allowed_words = "tracks, arcs, vias and copper pours" if allow_pours else "tracks, arcs and vias"
    if changed:
        banned = [line for line in changed if line.endswith("banned from routing")]
        beyond = [line for line in changed if line not in banned]
        if beyond:
            words.append(f"Not adopted: the board changed beyond {allowed_words}: " + "; ".join(beyond) + ".")
        if banned:
            words.append("Not adopted: " + "; ".join(banned) + ".")
    elif kept and summary and not problem:
        words.append(f"Adopted: the board after the last kept step, {allowed_words} changed only.")
    return words


def note(steps: list, summary, changed: list, attempt: str, problem: str = "", provider=None, unfinished=(), allow_pours: bool = False) -> str:
    """The record's note, from the results."""
    kept = sum(1 for step in steps if step.get("kept"))
    total = summary["tasks"] if summary else len(steps)
    before, after = checks_span(steps)
    words = [f"Routing run {attempt[:8]} in the Mac's PCB Editor: {kept} of {total} steps kept."]
    if problem:
        words.append(problem)
    words.append(
        f"Open connections {span(before, after, 'open')}, errors {span(before, after, 'errors')}, "
        f"findings {span(before, after, 'findings')}."
    )
    words.extend(_note_call_lines(steps, summary, provider))
    used = tokens(steps)
    if used is not None:
        words.append(f"Tokens: {used:,}.")
    else:
        words.append(
            "No usage total was available." if provider in ("codex", "gemini") else "Tokens: none counted (a stand-in agent)."
        )
    words.extend(_note_unkept_lines(steps))
    other = [
        f"{step.get('task')} ({step['calls']['other']})" for step in steps
        if isinstance(step.get("calls"), dict) and step["calls"].get("other")
    ]
    if other:
        words.append("Other commands than route, edit, look and date, read their transcripts: " + ", ".join(other) + ".")
    if unfinished:
        words.append("Unfinished tasks: " + ", ".join(unfinished) + ".")
    if summary:
        words.append(f"Stopped: {summary['stopped']}.")
    words.extend(_note_tail_lines(steps, summary, changed, kept, problem, allow_pours))
    return " ".join(words)


def evidence_lines(steps: list, summary, extra: dict) -> list:
    """The evidence file's lines: one per step with the runner's fields, then the summary with this run's facts."""
    lines = [json.dumps({key: step.get(key) for key in STEP_FIELDS}, sort_keys=False) for step in steps]
    clash = set(extra) & (set(SUMMARY_FIELDS) | {"summary"})
    if clash:
        raise ValueError(f"the run's facts would hide the runner's summary fields: {', '.join(sorted(clash))}")
    lines.append(json.dumps(dict({key: (summary or {}).get(key) for key in SUMMARY_FIELDS}, summary=True, **extra)))
    return lines


def results(text: str, strict=False) -> tuple:
    """The runner's output: its step lines and its summary line (None when it never printed one)."""
    steps, summary = [], None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            found = json.loads(line)
        except ValueError:
            if strict:
                return [], None
            continue
        if strict and summary is not None:
            return [], None
        if isinstance(found, dict) and "stopped" in found:
            summary = found
        elif isinstance(found, dict) and "task" in found:
            steps.append(found)
    return steps, summary


def run_state(said: str) -> dict:
    """The Mac's status lines about one run (STATUS's output) as a state: absent, waiting (for the editor), running,
    finished (with its exit code) or died (no exit and nothing alive)."""
    if said.strip() == "absent":
        return {"state": "absent"}
    fields = dict(line.split("=", 1) for line in said.splitlines() if "=" in line)
    found = {"phase": fields.get("phase", ""), "holder": fields.get("holder", "").strip(),
             "steps": int(fields.get("steps") or 0)}
    detail = fields.get("detail", "").strip()
    if detail:
        found["detail"] = detail
    if fields.get("exit", "").strip():
        return dict(found, state="finished", exit=int(fields["exit"]))
    if fields.get("alive") == "1":
        return dict(found, state="waiting" if found["phase"] == "waiting" else "running")
    return dict(found, state="died")
