#!/usr/bin/env python3
"""Design check, audit diffing, copper comparison, transcript parsing, and totals for editor_step."""
from __future__ import annotations

import collections
import hashlib
import json
import math
import pathlib
import re
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import editor_step_kicad  # noqa: E402
import kicad_rules_geom as rg  # noqa: E402
import kicad_sexp  # noqa: E402
import tool_paths  # noqa: E402
import validate_board  # noqa: E402
from routing_codex import _edit_verb, other_command_head, summarize_extra_tools, summarize_refusals  # noqa: E402
from routing_evidence import NO_ANSWER, route_quality_warning_detail  # noqa: E402

TOOL_ROOT = tool_paths.TOOL_ROOT
sys.path.insert(0, str(TOOL_ROOT / "tools" / "kicad"))
from kicad_install import KICAD_CLI  # noqa: E402

MANAGED_PROVIDERS = ("codex", "gemini")
CODEX_MODEL = "gpt-6.1-terra"
GEMINI_MODEL = "gemini-4-neon-medium"
GEMINI_REVIEW_MODEL = "gemini-4-neon-low"
ROUTING_MODEL = "claude-fable-5-2"
TOKEN_KINDS = ("input", "output", "thinking", "cache_read", "cache_write")
NO_AGENT = "none: a stand-in agent (--agent) ran, with no agent transcript to count"


def _facade():
    mod = sys.modules.get("editor_step")
    if mod is not None:
        return mod
    main_mod = sys.modules.get("__main__")
    if main_mod is not None and pathlib.Path(getattr(main_mod, "__file__", "") or "").name == "editor_step.py":
        return main_mod
    return sys.modules[__name__]


def design_check(path: pathlib.Path, out: pathlib.Path) -> dict:
    f = _facade()
    getattr(f, "subprocess", subprocess).run(
        [getattr(f, "KICAD_CLI", KICAD_CLI), "pcb", "drc", "--refill-zones", "--severity-all",
         "--format", "json", "--output", str(out), str(path)],
        capture_output=True, timeout=300,
    )
    found = json.loads(out.read_text())
    violations = found["violations"]
    isolated = sum(v.get("severity") == "warning" and v.get("type") == "isolated_copper" for v in violations)
    return {"open": len(found["unconnected_items"]),
            "errors": sum(v.get("severity") == "error" for v in violations),
            "findings": len(violations),
            **({"isolated_copper": isolated} if isolated else {})}


def check_passes(start: dict, end: dict, *, pours_changed: bool = False, audits_reduced: bool = False) -> bool:
    """Fewer open connections and no more errors or non-isolated DRC warnings, or no more of either and
    fewer design-check findings (or isolated_copper-only warnings when copper pours changed, or fewer audit findings)."""
    start_non_iso = start["findings"] - int(start.get("isolated_copper") or 0)
    end_non_iso = end["findings"] - int(end.get("isolated_copper") or 0)
    if end["open"] > start["open"] or end["errors"] > start["errors"] or end_non_iso > start_non_iso:
        return False
    if not pours_changed and end["findings"] > start["findings"]:
        return False
    return (
        end["open"] < start["open"]
        or end["findings"] < start["findings"]
        or pours_changed
        or audits_reduced
    )


def audits(path: pathlib.Path, folder: pathlib.Path, tag: str) -> list:
    """Every route quality and electrical finding on the board, one line each. A tool that gave no report is a finding.
    The electrical audit is the board's own, as its description names it; a board without one is not audited."""
    vb = getattr(_facade(), "validate_board", validate_board)
    found = []
    quality, electrical = folder / f"quality-{tag}.json", folder / f"electrical-{tag}.json"
    audit = vb.ELECTRICAL
    vb.kicad_offline("scripts/kicad_route_quality.py", "--board", path, "--output", quality, "--no-fail")
    if audit:
        vb.kicad_offline(audit["script"], "--board", path, "--json", electrical)
    for name, report in (("route quality", quality), *((("electrical audit", electrical),) if audit else ())):
        if not report.is_file():
            found.append(f"{name}: no report")
    if quality.is_file():
        found += [route_quality_warning_detail(item)
                  for item in json.loads(quality.read_text()).get("findings", [])]
    if electrical.is_file():
        found += [f"electrical audit {rule}: {line}" for rule, lines in json.loads(electrical.read_text()).get("findings", {}).items() for line in lines]
    return found


def new_findings(start: list, end: list) -> list:
    return sorted((collections.Counter(end) - collections.Counter(start)).elements())


def finding_details(folder, tag, added):
    """Show the matching geometry for new or moved audit findings, preserving legacy short-label matching."""
    report = folder / f"quality-{tag}.json"
    details = []
    remaining = collections.Counter(added)
    if report.is_file():
        for item in json.loads(report.read_text(encoding="utf-8"))["findings"]:
            detail = route_quality_warning_detail(item)
            label = f'route quality: {item["severity"]} {item["rule"]} on {item["net"]}'
            if remaining[detail] > 0:
                details.append(detail)
                remaining[detail] -= 1
            elif remaining[label] > 0:
                details.append(detail)
                remaining[label] -= 1
    return details


def _board_zones_summary(text: str) -> dict[str, list[tuple]]:
    """Canonical per-net summary of non-keepout copper zones (layers, outline/rule hash), ignoring fills and UUIDs."""
    if not text.strip():
        return {}
    try:
        root = kicad_sexp.parse(text)
    except ValueError:
        return {}
    if not isinstance(root, list) or not root or root[0] != "kicad_pcb":
        return {}
    net_map = rg.board_net_map(root)
    by_net: dict[str, list[tuple]] = collections.defaultdict(list)
    for z in kicad_sexp.children(root, "zone"):
        if kicad_sexp.child(z, "keepout"):
            continue
        net = rg.resolve_item_net(z, net_map)
        lnode = kicad_sexp.child(z, "layers") or kicad_sexp.child(z, "layer")
        layers = tuple(str(tok) for tok in (lnode[1:] if lnode else []))
        stripped = [
            part for part in z[1:]
            if not (isinstance(part, list) and part and part[0] in ("filled_polygon", "fill_segments", "uuid", "tstamp"))
        ]
        sig = hashlib.sha256(json.dumps(stripped).encode("utf-8")).hexdigest()[:12]
        by_net[net].append((layers, sig))
    for net in by_net:
        by_net[net].sort()
    return dict(by_net)


def copper(text: str) -> dict:
    """Each net's length, vias, pieces, and (when present) copper zones from a board file."""
    nets: dict = collections.defaultdict(lambda: {"length": 0.0, "vias": 0, "pieces": 0})
    for x0, y0, x1, y1, net in re.findall(r'\(segment\s+\(start ([\d.-]+) ([\d.-]+)\)\s+\(end ([\d.-]+) ([\d.-]+)\)[\s\S]*?\(net "([^"]*)"\)', text):
        nets[net]["length"] += math.hypot(float(x1) - float(x0), float(y1) - float(y0))
        nets[net]["pieces"] += 1
    for net in re.findall(r'\(via\b[\s\S]{0,300}?\(net "([^"]*)"\)', text):
        nets[net]["vias"] += 1
    for net, zlist in _board_zones_summary(text).items():
        nets[net]["zones"] = len(zlist)
        nets[net]["zone_layers"] = tuple(layer for layers, _ in zlist for layer in layers)
        nets[net]["zone_sigs"] = tuple(zlist)
    return nets


def _extract_copper_records(text: str) -> dict[str, dict] | None:
    """Per-net Counter records of segments, vias and non-keepout copper zones (ignoring zone fill polygons)."""
    if not text or not text.strip():
        return {}
    try:
        root = kicad_sexp.parse(text)
    except ValueError:
        return None
    if not isinstance(root, list) or not root or root[0] != "kicad_pcb":
        return None
    net_map = rg.board_net_map(root)

    by_net: dict[str, dict] = collections.defaultdict(
        lambda: {"segs": collections.Counter(), "vias": collections.Counter(), "zones": collections.Counter()}
    )
    for item in root[1:]:
        if not isinstance(item, list) or not item:
            continue
        kind = item[0]
        if kind == "segment":
            s_node = kicad_sexp.child(item, "start")
            e_node = kicad_sexp.child(item, "end")
            w_node = kicad_sexp.child(item, "width")
            l_node = kicad_sexp.child(item, "layer")
            if not (s_node and e_node and w_node and l_node):
                continue
            try:
                p0 = (round(float(s_node[1]), 4), round(float(s_node[2]), 4))
                p1 = (round(float(e_node[1]), 4), round(float(e_node[2]), 4))
                w = round(float(w_node[1]), 4)
            except (IndexError, TypeError, ValueError):
                continue
            a, b = (p0, p1) if p0 <= p1 else (p1, p0)
            by_net[rg.resolve_item_net(item, net_map)]["segs"][(str(l_node[1]), w, a, b)] += 1
        elif kind == "via":
            at_node = kicad_sexp.child(item, "at")
            if not at_node or len(at_node) < 3:
                continue
            s_node = kicad_sexp.child(item, "size")
            d_node = kicad_sexp.child(item, "drill")
            l_node = kicad_sexp.child(item, "layers")
            try:
                x = round(float(at_node[1]), 4)
                y = round(float(at_node[2]), 4)
                sz = round(float(s_node[1]), 4) if s_node and len(s_node) >= 2 else 0.0
                dr = round(float(d_node[1]), 4) if d_node and len(d_node) >= 2 else 0.0
            except (IndexError, TypeError, ValueError):
                continue
            layers = tuple(str(tok) for tok in (l_node[1:] if l_node else ()))
            by_net[rg.resolve_item_net(item, net_map)]["vias"][(x, y, sz, dr, layers)] += 1
        elif kind == "zone" and not kicad_sexp.child(item, "keepout"):
            l_node = kicad_sexp.child(item, "layers") or kicad_sexp.child(item, "layer")
            layers = tuple(str(tok) for tok in (l_node[1:] if l_node else ()))
            pts = []
            for poly in kicad_sexp.children(item, "polygon"):
                pts_node = kicad_sexp.child(poly, "pts")
                if pts_node:
                    for xy in kicad_sexp.children(pts_node, "xy"):
                        if len(xy) >= 3:
                            try:
                                pts.append((round(float(xy[1]), 4), round(float(xy[2]), 4)))
                            except (TypeError, ValueError):
                                pass
            bbox = (
                (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))
                if pts else ()
            )
            filled_layers = tuple(sorted({
                str(ln[1])
                for fp in kicad_sexp.children(item, "filled_polygon")
                for ln in (kicad_sexp.child(fp, "layer"),)
                if ln and len(ln) >= 2 and kicad_sexp.child(fp, "pts") is not None
            }))
            by_net[rg.resolve_item_net(item, net_map)]["zones"][(layers, bbox, filled_layers)] += 1
    return dict(by_net)


def changed_copper_details(before: str, after: str, *, max_items: int = 80) -> str:
    """Compact per-net list of added/removed segments, vias and zones (excluding filled_polygon bulk)."""
    b_map = _extract_copper_records(before)
    a_map = _extract_copper_records(after)
    if b_map is None or a_map is None:
        return ""
    empty = {"segs": collections.Counter(), "vias": collections.Counter(), "zones": collections.Counter()}
    lines: list[str] = []
    emitted = 0
    truncated = 0

    def _push(entry: str) -> None:
        nonlocal emitted, truncated
        if emitted < max_items:
            lines.append(entry)
            emitted += 1
        else:
            truncated += 1

    for net in sorted(set(b_map) | set(a_map)):
        b_rec = b_map.get(net, empty)
        a_rec = a_map.get(net, empty)
        add_s, rem_s = a_rec["segs"] - b_rec["segs"], b_rec["segs"] - a_rec["segs"]
        add_v, rem_v = a_rec["vias"] - b_rec["vias"], b_rec["vias"] - a_rec["vias"]
        add_z, rem_z = a_rec["zones"] - b_rec["zones"], b_rec["zones"] - a_rec["zones"]
        if not (add_s or rem_s or add_v or rem_v or add_z or rem_z):
            continue
        header = (
            f"Net {net or '<no-net>'}: +{sum(add_s.values())}/-{sum(rem_s.values())} segments, "
            f"+{sum(add_v.values())}/-{sum(rem_v.values())} vias"
        )
        if add_z or rem_z:
            header += f", +{sum(add_z.values())}/-{sum(rem_z.values())} zones"
        lines.append(header)
        for sign, counter in (("+", add_z), ("-", rem_z)):
            for layers, bbox, filled in sorted(counter.elements()):
                btxt = f"[{bbox[0]:g},{bbox[1]:g}..{bbox[2]:g},{bbox[3]:g}]" if bbox else "[]"
                _push(f"  {sign} zone ({','.join(layers) or '?'}) filled=({','.join(filled) or 'none'}) bbox={btxt}")
        for sign, counter in (("+", add_s), ("-", rem_s)):
            for layer, w, p0, p1 in sorted(counter.elements()):
                _push(f"  {sign} seg {layer} w={w:g} ({p0[0]:g},{p0[1]:g})->({p1[0]:g},{p1[1]:g})")
        for sign, counter in (("+", add_v), ("-", rem_v)):
            for x, y, sz, dr, layers in sorted(counter.elements()):
                _push(f"  {sign} via ({x:g},{y:g}) d={sz:g}/{dr:g} ({','.join(layers) or '*'})")
    if truncated:
        lines.append(f"  ... (+{truncated} more copper items in saved PCB)")
    return "\n".join(lines)


def moved_table(before: str, after: str) -> str:
    start, end = copper(before), copper(after)
    rows = []
    for net in sorted(set(start) | set(end)):
        s, e = start[net], end[net]
        if s == e:
            continue
        desc = (
            f"{net}: length {s['length']:.1f} to {e['length']:.1f} mm, vias {s['vias']} to {e['vias']}, "
            f"pieces {s['pieces']} to {e['pieces']}"
        )
        sz, ez = s.get("zones", 0), e.get("zones", 0)
        if sz or ez or s.get("zone_sigs") != e.get("zone_sigs"):
            layers_after = ",".join(dict.fromkeys(e.get("zone_layers", ()))) or "none"
            desc += f", zones {sz} to {ez} ({layers_after})"
        rows.append(desc)
    return "\n".join(rows) or "nothing"


def tokens(log: pathlib.Path) -> dict:
    """What one agent's log says it used: each round's own count, and the session's totals per model from its last result line."""
    rounds, by_model = [], {}
    if not log.is_file():
        return {"rounds": rounds, "by_model": by_model}
    for line in log.read_text(errors="replace").splitlines():
        if '"type":"result"' not in line:
            continue
        try:
            found = json.loads(line)
        except ValueError:
            continue
        if not isinstance(found, dict) or found.get("type") != "result":
            continue
        usage = found.get("usage") or {}
        rounds.append({"turns": found.get("num_turns"), "seconds": round((found.get("duration_ms") or 0) / 1000),
                       "input": usage.get("input_tokens"), "output": usage.get("output_tokens"),
                       "thinking": (usage.get("output_tokens_details") or {}).get("thinking_tokens"),
                       "cache_read": usage.get("cache_read_input_tokens"), "cache_write": usage.get("cache_creation_input_tokens")})
        by_model = {model: {"input": used.get("inputTokens"), "output": used.get("outputTokens"), "thinking": used.get("thinkingTokens"),
                            "cache_read": used.get("cacheReadInputTokens"), "cache_write": used.get("cacheCreationInputTokens")}
                    for model, used in (found.get("modelUsage") or {}).items()}
    return {"rounds": rounds, "by_model": by_model}


def tokens_total(used: dict) -> dict:
    """Each kind of token summed over the agent's rounds and every review's."""
    return {kind: sum(one.get(kind) or 0 for record in [used["agent"], *used["reviews"]] for one in record["rounds"])
            for kind in TOKEN_KINDS}


def known_total(used):
    if all(record.get("complete", True) for record in [used["agent"], *used["reviews"]]):
        return tokens_total(used)
    return None


def route_status(text: str) -> str:
    """The status a route tool call returned, from its output: the last JSON object with a status in it."""
    status = ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                found = json.loads(line)
            except ValueError:
                continue
            if isinstance(found, dict) and found.get("status"):
                status = str(found["status"])
    return status or ("usage" if "usage: kicad_route.py" in text else "no_status")


def calls(lines) -> dict:
    """What an agent ran, from its stream-json transcript's lines."""
    commands, results = [], {}
    for line in lines:
        try:
            found = json.loads(line)
        except ValueError:
            continue
        message = found.get("message") if isinstance(found, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        for part in content if isinstance(content, list) else []:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "tool_use" and part.get("name") == "Bash":
                commands.append((part.get("id"), str((part.get("input") or {}).get("command", "")).strip()))
            elif part.get("type") == "tool_result":
                text = part.get("content")
                if isinstance(text, list):
                    text = "\n".join(str(piece.get("text", "")) for piece in text if isinstance(piece, dict))
                results[part.get("tool_use_id")] = str(text or "")

    def _find_script(words: list[str], name: str) -> int | None:
        suffix = f"scripts/{name}"
        for idx, word in enumerate(words):
            if word == suffix or word.endswith("/" + suffix):
                return idx
        return None

    route, edit, lay, other_by_cmd = collections.Counter(), collections.Counter(), collections.Counter(), collections.Counter()
    looks = other = ipc_copper_calls = gui_copper_calls = 0
    for call_id, command in commands:
        words = command.split()
        if _find_script(words, "kicad_route.py") is not None:
            status = route_status(results[call_id]) if call_id in results else "no_result"
            route[status] += 1
            if status != "usage":
                gui_copper_calls += 1
        elif (idx := _find_script(words, "kicad_lay.py")) is not None:
            verb = _edit_verb(words[idx + 1:])
            status = route_status(results[call_id]) if call_id in results else "no_result"
            lay[status] += 1
            if verb not in ("pads", "zones", "none") and status != "usage":
                ipc_copper_calls += 1
        elif (idx := _find_script(words, "kicad_edit.py")) is not None:
            verb = _edit_verb(words[idx + 1:])
            edit[verb] += 1
        elif (idx := _find_script(words, "mac.py")) is not None and words[idx + 1:][:1] == ["look"]:
            looks += 1
        else:
            other += 1
            other_by_cmd[other_command_head(command)] += 1
    repeated = collections.Counter(command for _, command in commands)
    return {"route": sum(route.values()), "route_closed": route["closed"],
            "route_refused": sum(n for status, n in route.items() if status not in ("closed", "usage", "no_result")),
            "route_by_status": dict(sorted(route.items())),
            "lay": sum(lay.values()), "lay_laid": lay["laid"] + lay["deleted"],
            "lay_refused": sum(n for status, n in lay.items() if status not in ("laid", "deleted", "done", "ok", "usage", "no_result")),
            "lay_by_status": dict(sorted(lay.items())),
            "ipc_copper_calls": ipc_copper_calls, "gui_copper_calls": gui_copper_calls,
            "edit": sum(edit.values()), "edit_by_verb": dict(sorted(edit.items())),
            "look": looks, "other": other, "other_by_cmd": dict(sorted(other_by_cmd.items())),
            "repeats": {command: n for command, n in repeated.most_common() if n > 1}}


def decide(path, start_check, start_audits, proc, socket, watched, finished, folder, tag, *,
           before_zones=None) -> tuple[dict, list | None]:
    """What happened to the board, from the file and the editor only, and the audits' findings when they ran."""
    f = _facade()
    if watched:
        return {"passed": False, "why": "file_changed", "detail": watched[0]}, None
    if not finished:
        return {"passed": False, "why": "timeout"}, None
    if proc.poll() is not None:
        return {"passed": False, "why": "editor_closed"}, None
    lock_fn = getattr(f, "lock", editor_step_kicad.lock)
    editor_board_fn = getattr(f, "editor_board", editor_step_kicad.editor_board)
    same_board_fn = getattr(f, "same_board", editor_step_kicad.same_board)
    lock_fn(path, False)
    try:
        held = editor_board_fn(socket)  # before saving: a save would write whatever board the editor holds
        if same_board_fn(held, path):
            editor_board_fn(socket, save=True, refill=True)
    except Exception as error:
        detail = str(error)[-300:]
        why = "editor_busy" if "busy" in detail.lower() else "editor_would_not_save"
        return {"passed": False, "why": why, "detail": detail}, None
    if not same_board_fn(held, path):
        return {"passed": False, "why": "editor_holds_another_file", "detail": str(held)}, None
    after = f.design_check(path, folder / "drc-end.json")
    pours_changed = (before_zones is not None and _board_zones_summary(path.read_text(encoding="utf-8")) != before_zones)
    end_audits = None
    check_fn = getattr(f, "check_passes", check_passes)
    passed_check = check_fn(start_check, after, pours_changed=pours_changed)
    start_non_iso = start_check["findings"] - int(start_check.get("isolated_copper") or 0)
    end_non_iso = after["findings"] - int(after.get("isolated_copper") or 0)
    if (
        not passed_check
        and start_audits
        and after["open"] <= start_check["open"]
        and after["errors"] <= start_check["errors"]
        and end_non_iso <= start_non_iso
        and after["findings"] <= start_check["findings"]
    ):
        end_audits = f.audits(path, folder, tag)
        passed_check = check_fn(
            start_check,
            after,
            pours_changed=pours_changed,
            audits_reduced=(len(end_audits) < len(start_audits)),
        )
    if not passed_check:
        return {"passed": False, "why": "check_failed", "before": start_check, "after": after}, None
    if end_audits is None:
        end_audits = f.audits(path, folder, tag)
    added = f.new_findings(start_audits, end_audits)
    details = f.finding_details(folder, tag, added) if added else []
    return {"passed": not added, "why": "audit_failed" if added else "editor_work", "before": start_check, "after": after,
            "new_findings": added, "matching_geometry": details}, end_audits


def _effective_review_provider(args) -> str:
    return getattr(args, "review_provider", None) or args.provider


def _effective_review_model(args) -> str:
    if args.review_model:
        return args.review_model
    review_provider = _effective_review_provider(args)
    if review_provider == "gemini":
        return GEMINI_REVIEW_MODEL
    if review_provider == args.provider and args.model:
        return args.model
    return {"codex": CODEX_MODEL, "gemini": GEMINI_REVIEW_MODEL, "claude": ROUTING_MODEL}[review_provider]


def step_evidence(args, transcript, review_logs, board_stem=None):
    f = _facade()
    calls_fn = getattr(f, "calls", calls)
    tokens_fn = getattr(f, "tokens", tokens)
    if args.provider == "codex":
        import routing_codex
        agent_tokens = routing_codex.tokens(transcript, args.model)
        ran_calls = routing_codex.calls(transcript, board_stem=board_stem)
    elif args.provider == "gemini":
        import routing_gemini
        agent_tokens = routing_gemini.tokens(transcript, args.model)
        ran_calls = routing_gemini.calls(transcript, board_stem=board_stem)
    else:
        agent_tokens = tokens_fn(transcript)
        ran_calls = calls_fn(transcript.read_text(errors="replace").splitlines() if transcript.is_file() else [])
    review_provider = _effective_review_provider(args)
    review_model = _effective_review_model(args)
    if review_provider == "codex":
        import routing_codex
        review_tokens = [routing_codex.tokens(log, review_model) for log in review_logs]
    elif review_provider == "gemini":
        import routing_gemini
        review_tokens = [routing_gemini.tokens(log, review_model) for log in review_logs]
    else:
        review_tokens = [tokens_fn(log) for log in review_logs]
    used = {"agent": agent_tokens, "reviews": review_tokens}
    return used, ran_calls


def _step_timing(args, transcript: pathlib.Path, used: dict, ran, total_s: float,
                 operator_wall_s: float, review_wall_s: float) -> dict:
    """Build strictly passive step timing breakdown without imposing thresholds or gates."""
    provider = getattr(args, "provider", None)
    thinking_tokens = 0
    tool_s = float(ran.get("tool_s") or ran.get("tool_seconds") or 0.0) if isinstance(ran, dict) else 0.0
    op_s = float(operator_wall_s)
    if provider == "gemini" and transcript.is_file():
        import routing_gemini
        timing_fn = getattr(routing_gemini, "session_timing", None)
        if callable(timing_fn):
            gt = timing_fn(transcript)
            tool_s = float(gt.get("tool_s") or 0.0)
            thinking_tokens = int(gt.get("thinking_tokens") or 0)
            if op_s <= 0.0:
                op_s = float(gt.get("wall_s") or 0.0)
    if not thinking_tokens and isinstance(used, dict):
        agent_rounds = (used.get("agent") or {}).get("rounds") or []
        thinking_tokens = sum(int(r.get("thinking") or 0) for r in agent_rounds if isinstance(r, dict))
        if not thinking_tokens:
            kt = known_total(used)
            if isinstance(kt, dict) and isinstance(kt.get("thinking"), int):
                thinking_tokens = kt["thinking"]
    llm_r = round(max(0.0, op_s - tool_s), 1)
    tool_r = round(max(0.0, tool_s), 1)
    rev_r = round(max(0.0, review_wall_s), 1)
    harness_r = round(max(0.0, float(total_s) - op_s - review_wall_s), 1)
    refusal_r = round(float(ran.get("refusal_seconds") or 0.0), 1) if isinstance(ran, dict) else 0.0
    return {
        "total_s": round(max(0.0, float(total_s)), 1),
        "llm_s": llm_r,
        "tool_s": tool_r,
        "review_s": rev_r,
        "harness_s": harness_r,
        "refusal_s": refusal_r,
        "thinking_tokens": int(thinking_tokens),
    }


def run_totals(results: list) -> dict:
    """The summary's tokens, route calls, lay calls, timing and passive refusal metrics across steps."""
    counted = [result for result in results if isinstance(result["calls"], dict)]
    if not counted:
        return {"tokens_total": NO_AGENT, "route_calls": NO_AGENT, "route_closed": NO_AGENT, "route_refused": NO_AGENT}
    total = None
    if all(isinstance(result["tokens_total"], dict) for result in counted):
        total = {kind: sum(result["tokens_total"][kind] for result in counted) for kind in TOKEN_KINDS}
    totals: dict = {"tokens_total": total,
                    "route_calls": sum(result["calls"]["route"] for result in counted),
                    "route_closed": sum(result["calls"]["route_closed"] for result in counted),
                    "route_refused": sum(result["calls"]["route_refused"] for result in counted)}
    if any(result["calls"].get("lay") for result in counted):
        totals.update(
            lay_calls=sum(result["calls"].get("lay", 0) for result in counted),
            lay_laid=sum(result["calls"].get("lay_laid", 0) for result in counted),
            lay_refused=sum(result["calls"].get("lay_refused", 0) for result in counted),
        )
    if any(result.get("routing_mode") == "autoroute" or result["calls"].get("autoroute") for result in counted):
        totals["autoroute_steps"] = sum(
            1 for result in counted if result.get("routing_mode") == "autoroute" or result["calls"].get("autoroute")
        )
    if any(result.get("provider") in MANAGED_PROVIDERS and result["calls"].get("editor_calls") for result in counted):
        gestures = collections.Counter()
        for result in counted:
            gestures.update(result["calls"].get("editor_calls", {}))
        totals["editor_calls"] = dict(gestures)
    timed = [result["timing"] for result in results if isinstance(result.get("timing"), dict)]
    if timed:
        totals["timing"] = {
            "total_s": round(sum(float(t.get("total_s") or 0.0) for t in timed), 1),
            "llm_s": round(sum(float(t.get("llm_s") or 0.0) for t in timed), 1),
            "tool_s": round(sum(float(t.get("tool_s") or 0.0) for t in timed), 1),
            "review_s": round(sum(float(t.get("review_s") or 0.0) for t in timed), 1),
            "harness_s": round(sum(float(t.get("harness_s") or 0.0) for t in timed), 1),
            "refusal_s": round(sum(float(t.get("refusal_s") or 0.0) for t in timed), 1),
            "thinking_tokens": sum(int(t.get("thinking_tokens") or 0) for t in timed),
        }
    refusals = summarize_refusals([result["calls"] for result in counted])
    if refusals is not None:
        totals["refusals"] = refusals
    extra_tools = summarize_extra_tools([result["calls"] for result in counted])
    if extra_tools is not None:
        totals["extra_tools"] = extra_tools
    return totals
