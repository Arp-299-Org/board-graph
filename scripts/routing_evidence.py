"""One review contract at the reviewer, download and committed-copper boundaries."""
from __future__ import annotations
import collections
import json
import pathlib
import re

import kicad_rules_geom as rg
import kicad_sexp


def review_findings(earlier: list, answer: str, pads=(), warnings=()) -> list:
    """Accept an explicit assessment of the pictures. An error message or unrelated prose cannot clear review."""
    seen = set()
    lines = [line.strip() for line in answer.splitlines() if line.strip()]
    if not lines:
        raise ValueError("Review gave no answer")
    observed = set()
    pad_findings = []
    for line in lines:
        if line.startswith("PAD "):
            label, sep, observation = line[4:].partition(":")
            if label not in pads or label in observed or not sep or not observation.strip():
                raise ValueError(f"Invalid pad observation: {line}")
            observed.add(label)
            marker = re.search(r"\bMUST FIX\s*:", observation, flags=re.IGNORECASE)
            if marker:
                detail = observation[marker.end():].strip()
                if not detail:
                    raise ValueError(f"Missing pad finding detail: {line}")
                pad_findings.append(f"MUST FIX: {label}: {detail}")
            continue
        if line.rstrip(".") == "NOTHING" or re.fullmatch(r"(?:MUST FIX|FINE AS IS):\s*\S.*", line):
            continue
        found = re.fullmatch(r"(?:FIXED|STILL OPEN) (\d+):\s*\S.*", line)
        if not found or not 1 <= int(found[1]) <= len(earlier) or int(found[1]) in seen:
            raise ValueError(f"Cannot verify the review verdict: {line[:300]}")
        seen.add(int(found[1]))
    if observed != set(pads) or (pads and len(lines) == len(observed)):
        raise ValueError("Review requires an observation for each pad and an explicit verdict")
    findings = list(dict.fromkeys(still_open(earlier, answer) + pad_findings))
    if warnings and not findings:
        accepted = []
        for line in lines:
            if line.startswith("FINE AS IS: Warning "):
                match = re.fullmatch(r"FINE AS IS: Warning ([1-9][0-9]*):\s*(\S.*)", line)
                if not match:
                    raise ValueError(f"Missing warning number or reason: {line}")
                accepted.append(int(match[1]))
        if sorted(accepted) != list(range(1, len(warnings) + 1)):
            raise ValueError("Review requires one numbered FINE AS IS with a reason for every route-quality warning")
    return findings


NO_ANSWER = "MUST FIX: the review gave no answer (it stopped or ran out of time): a review that did not answer is not a pass"


def still_open(earlier: list, answer: str) -> list:
    """What a review leaves open: each earlier finding it does not call FIXED by its number (silence is not a pass),
    then its own MUST FIX lines. A review with no answer at all leaves everything open and says so."""
    if not answer.strip():
        return list(earlier) + ([] if NO_ANSWER in earlier else [NO_ANSWER])
    fixed ={int(n) for n in re.findall(r"^[\s*`>-]*FIXED\W*(\d+)", answer, re.M)}
    carried = [finding for n, finding in enumerate(earlier, 1) if n not in fixed]
    return carried + [line.strip() for line in answer.splitlines() if line.strip().startswith("MUST FIX") and line.strip() not in carried]


ROUTING_MODES = ("interactive", "ipc", "hybrid")
LEGACY_ROUTING_MODES = ("autoroute",)
DEFAULT_ROUTING_MODE = "hybrid"


def parse_task_mode(text: str) -> str | None:
    """Parse an optional Mode: interactive|ipc|hybrid header line."""
    mode_lines = [line.strip() for line in text.splitlines() if line.strip().lower().startswith("mode:")]
    if not mode_lines:
        return None
    if len(mode_lines) > 1:
        raise ValueError("Use Mode: interactive|ipc|hybrid at most once")
    value = mode_lines[0].split(":", 1)[1].strip().lower()
    if value not in ROUTING_MODES:
        raise ValueError(f"Unsupported routing mode {value!r}; use one of: {', '.join(ROUTING_MODES)}")
    return value


def effective_task_mode(text: str) -> str:
    return parse_task_mode(text) or DEFAULT_ROUTING_MODE


def parse_task_connections(text: str) -> list[list[str]]:
    """Parse consecutive Connection: FROM_REF.PAD TO_REF.PAD NET lines at the top of a routing task."""
    lines = text.strip().splitlines()
    targets: list[list[str]] = []
    seen_pairs: set[frozenset[str]] = set()
    idx = 0
    while idx < len(lines) and lines[idx].strip().lower().startswith(("mode:", "pour:")):
        idx += 1
    while idx < len(lines) and lines[idx].startswith("Connection:"):
        fields = lines[idx][len("Connection:"):].split()
        if len(fields) != 3 or any("." not in pad for pad in fields[:2]) or fields[0] == fields[1]:
            raise ValueError("Use Connection: FROM_REF.PAD TO_REF.PAD NET at the top of the task")
        pair_key = frozenset(fields[:2])
        if pair_key in seen_pairs:
            raise ValueError(f"Duplicate Connection: line for {' '.join(fields)}")
        seen_pairs.add(pair_key)
        targets.append(fields)
        idx += 1
    for line in lines[idx:]:
        if line.lstrip().startswith("Connection:"):
            raise ValueError("Place all Connection: lines consecutively at the top of the task")
    return targets


def parse_task_pours(text: str) -> list[dict]:
    """Parse consecutive Pour: LAYERS NET lines at the top of a routing task."""
    lines = text.strip().splitlines()
    pours: list[dict] = []
    seen: set[tuple[tuple[str, ...], str]] = set()
    idx = 0
    while idx < len(lines) and lines[idx].strip().lower().startswith(("mode:", "connection:", "pad exits:")):
        idx += 1
    while idx < len(lines) and lines[idx].startswith("Pour:"):
        fields = lines[idx][len("Pour:"):].split()
        if len(fields) != 2:
            raise ValueError("Use Pour: LAYERS NET at the top of the task (comma-separated copper layers)")
        layers = [part.strip() for part in fields[0].split(",")]
        if (not layers or any(not layer.endswith(".Cu") or len(layer) <= 3 or any(ch in layer for ch in "*&/")
                              for layer in layers)):
            raise ValueError(f"Invalid copper layer list in Pour: line: {fields[0]!r}")
        if len(set(layers)) != len(layers):
            raise ValueError(f"Duplicate layer in Pour: line: {fields[0]!r}")
        net = fields[1]
        key = (tuple(layers), net)
        if key in seen:
            raise ValueError(f"Duplicate Pour: line for {' '.join(fields)}")
        seen.add(key)
        pours.append({"layers": layers, "net": net})
        idx += 1
    for line in lines[idx:]:
        if line.lstrip().startswith("Pour:"):
            raise ValueError("Place all Pour: lines consecutively at the top of the task")
    return pours


def _expand_zone_layers(layer_tokens: list[str], board_copper: list[str]) -> set[str]:
    return set(rg.expand_copper_layers(layer_tokens, board_copper))


def _zone_net_name(zone_node, net_map: dict[str, str]) -> str:
    return rg.resolve_item_net(zone_node, net_map)


def saved_pours(board_text: str, declared_pours: list[dict], saved_sha: str) -> list[dict]:
    """Check each declared copper pour against non-keepout filled zones in a saved .kicad_pcb."""
    root = kicad_sexp.parse(board_text)
    if not isinstance(root, list) or not root or root[0] != "kicad_pcb":
        raise ValueError("not a KiCad board")
    board_copper = kicad_sexp.copper_layers(root) or ["F.Cu", "B.Cu"]
    net_map = rg.board_net_map(root)
    copper_zones = []
    for zone in kicad_sexp.children(root, "zone"):
        if kicad_sexp.child(zone, "keepout"):
            continue
        layers_node = kicad_sexp.child(zone, "layers") or kicad_sexp.child(zone, "layer")
        zone_layers = _expand_zone_layers(
            [str(tok) for tok in (layers_node[1:] if layers_node else [])],
            board_copper,
        )
        zone_net = _zone_net_name(zone, net_map)
        filled_by_layer: collections.Counter = collections.Counter()
        for fp in kicad_sexp.children(zone, "filled_polygon"):
            layer_node = kicad_sexp.child(fp, "layer")
            pts_node = kicad_sexp.child(fp, "pts")
            if layer_node and len(layer_node) >= 2 and pts_node and len(kicad_sexp.children(pts_node, "xy")) >= 3:
                filled_by_layer[str(layer_node[1])] += 1
        copper_zones.append((zone_net, zone_layers, filled_by_layer))

    results: list[dict] = []
    for spec in declared_pours:
        req_layers = list(spec["layers"])
        req_net = str(spec["net"])
        matched = [
            filled for z_net, z_layers, filled in copper_zones
            if z_net == req_net and set(req_layers).issubset(z_layers)
        ]
        filled_layers = {layer for filled in matched for layer, count in filled.items() if count > 0}
        polys = sum(filled.get(layer, 0) for filled in matched for layer in req_layers)
        results.append({
            "layers": req_layers,
            "net": req_net,
            "zone_count": len(matched),
            "filled": bool(matched) and all(layer in filled_layers for layer in req_layers),
            "filled_polygons": polys,
            "saved_sha256": saved_sha,
        })
    return results


def parse_task_pad_exits(text: str, targets: list[list[str]]) -> bool:
    """Return True when the task requests centered exits below one or more Connection: lines."""
    exit_lines = [line.strip() for line in text.splitlines() if line.strip().lower().startswith("pad exits:")]
    if not exit_lines:
        return False
    if not targets or exit_lines != ["Pad exits: centered"]:
        raise ValueError("Use Pad exits: centered once, below a named Connection: line")
    return True


def task_contract(text: str) -> tuple[list[list[str]], bool]:
    targets = parse_task_connections(text)
    return targets, parse_task_pad_exits(text, targets)


def step_connections(step: dict) -> list[dict]:
    """Return the step receipt's named connection records (single connection or non-empty connections list)."""
    one = step.get("connection")
    many = step.get("connections")
    if one is not None and many is not None:
        raise ValueError("Step receipt cannot contain both connection and connections")
    if many is not None:
        if not isinstance(many, list) or not many or not all(isinstance(c, dict) for c in many):
            raise ValueError("Invalid connections list in step receipt")
        return many
    if one is not None:
        if not isinstance(one, dict):
            raise ValueError("Invalid connection entry in step receipt")
        return [one]
    return []


PROVIDERS = ("claude", "codex", "gemini")


def adoption_problem(steps, summary, final_sha, provider, expected_tasks=None, review_provider=None):
    """Check the complete kept prefix and its final board, including after a resumed download."""
    expected_review = review_provider or (summary.get("review_provider") if isinstance(summary, dict) else None) or provider
    if not isinstance(summary, dict) or not steps or provider not in PROVIDERS or expected_review not in PROVIDERS:
        return "Missing completed routing batch or independent routing provider"
    if not isinstance(steps, list) or not all(isinstance(s, dict) for s in steps):
        return "Invalid routing step receipt"
    tasks = [s.get("task") for s in steps]
    if any(not isinstance(t, str) or not t for t in tasks) or len(set(tasks)) != len(tasks):
        return "Missing or repeated task in the batch receipt"
    if expected_tasks is not None and (tasks != list(expected_tasks)[:len(tasks)] or summary.get("tasks") != len(expected_tasks)):
        return "Routing receipt differs from the committed assignment"
    kept = [s for s in steps if s.get("kept") is True]
    if (not kept or steps[:len(kept)] != kept or any(type(s.get("kept")) is not bool for s in steps)
            or type(summary.get("kept")) is not int or summary["kept"] != len(kept)
            or type(summary.get("tasks")) is not int or summary["tasks"] < len(steps)):
        return "Batch summary and kept steps disagree"
    if len(kept) == len(steps) < summary["tasks"]:
        stopped = summary.get("stopped")
        between = (stopped in ("Routing card deadline reached", "the Mac's screen is locked")
                   or isinstance(stopped, str) and stopped.startswith("editor not ready: "))
        if expected_tasks is None or not between or summary.get("outcome", "needs_lead") != "needs_lead":
            return "Missing a between-task stop for the unfinished assignment"
    for step in kept:
        proof = step.get("review_receipt")
        if not isinstance(proof, dict) or proof.get("schema") != "board-graph/routing-review/v1":
            return "Missing independent review receipt; preserve this board for inspection"
        board = step.get("reviewed_sha256")
        if not isinstance(board, str) or not re.fullmatch("[a-f0-9]{64}", board) or proof.get("board_sha256") != board:
            return "Review receipt and saved board fingerprint disagree"
        if (type(step.get("reviews")) is not int or step["reviews"] <= 0 or step.get("review_open") != []
                or step.get("provider") != provider
                or (step.get("review_provider") if step.get("review_provider") is not None else step.get("provider")) != expected_review
                or not isinstance(step.get("review_model"), str) or not step["review_model"] or proof.get("reviewer") != step["review_model"]):
            return "Kept step lacks a completed independent review with no open findings"
        mode = step.get("routing_mode")
        if mode is not None and mode not in (*ROUTING_MODES, *LEGACY_ROUTING_MODES):
            return "Invalid routing mode in step receipt"
        if mode == "autoroute":
            ar = step.get("autoroute")
            if not isinstance(ar, dict) or not isinstance(ar.get("solver"), str) or not ar["solver"].strip():
                return "Autoroute step receipt is missing solver provenance"
        try:
            conns = step_connections(step)
        except ValueError:
            return "Named connection does not match the saved reviewed board"
        for pair in conns:
            if pair.get("before") is not False or pair.get("after") is not True or pair.get("saved_sha256") != board:
                return "Named connection does not match the saved reviewed board"
        step_pours = step.get("pours")
        if step_pours is not None:
            if not isinstance(step_pours, list) or not all(
                isinstance(p, dict) and isinstance(p.get("layers"), list) and p["layers"]
                and all(isinstance(layer, str) and layer for layer in p["layers"])
                and isinstance(p.get("net"), str) and p["net"]
                and type(p.get("zone_count")) is int and p["zone_count"] >= 1
                and p.get("filled") is True
                and type(p.get("filled_polygons")) is int and p["filled_polygons"] >= len(p["layers"])
                and p.get("saved_sha256") == board
                for p in step_pours
            ):
                return "Saved pour does not match the filled copper zone on the reviewed board"
        warnings, previous, answer = proof.get("warnings"), proof.get("earlier"), proof.get("answer")
        if (not isinstance(warnings, list) or not isinstance(previous, list) or not isinstance(answer, str)
                or not all(isinstance(v, str) and v for v in warnings + previous)):
            return "Incomplete independent review response"
        if step.get("why") != ("warning_accepted" if warnings else "editor_work"):
            return "Kept status contradicts the saved check result"
        added = step.get("new_findings") or []
        if (any(not isinstance(v, str) or not v.startswith("route quality: warning ") for v in added)
                or bool(added) != bool(warnings) or warnings != (step.get("matching_geometry") or [])
                or any(not any(w == v or w.startswith(v + ",") for w in warnings) for v in added)):
            return "Warning dispositions do not cover the saved findings"
        pads = step.get("pad_exits") or []
        if (not isinstance(pads, list)
                or any(not isinstance(p, dict) or p.get("issues") != [] or not isinstance(p.get("pad"), str) for p in pads)
                or len(pads) != len({p["pad"] for p in pads})):
            return "Saved pad geometry has open or missing findings"
        if isinstance(expected_tasks, dict):
            try:
                targets, centered = task_contract(expected_tasks[step["task"]])
                expected_pours = parse_task_pours(expected_tasks[step["task"]])
                declared_mode = parse_task_mode(expected_tasks[step["task"]])
            except ValueError:
                return "Saved connection differs from the assigned pads and net"
            if declared_mode is not None and mode != declared_mode:
                return "Routing mode in receipt differs from the assigned task"
            if targets and [[c.get(k) for k in ("from", "to", "net")] for c in conns] != targets:
                return "Saved connection differs from the assigned pads and net"
            if expected_pours and (
                not isinstance(step_pours, list)
                or [{"layers": p["layers"], "net": p["net"]} for p in step_pours] != expected_pours
            ):
                return "Saved pour differs from the assigned layers and net"
            expected_pads = sorted({pad for t in targets for pad in t[:2]})
            if centered and (not expected_pads or len(pads) != len(expected_pads)
                             or sorted(p["pad"] for p in pads) != expected_pads):
                return "Missing centered-exit observations for the assigned pair"
        try:
            if review_findings(previous, answer, [p["pad"] for p in pads], warnings):
                return "Independent review still has unresolved findings"
        except ValueError as error:
            return str(error)
    earlier_pairs = {}
    earlier_pours = {}
    for s in kept[:-1]:
        if s.get("connections") is not None:
            earlier_pairs[s["task"]] = [[c.get(k) for k in ("from", "to", "net")] for c in s["connections"]]
        elif s.get("connection") is not None:
            earlier_pairs[s["task"]] = [s["connection"].get(k) for k in ("from", "to", "net")]
        if s.get("pours"):
            earlier_pours[s["task"]] = [{"layers": p["layers"], "net": p["net"]} for p in s["pours"]]
    final = summary.get("final_connections")
    if (earlier_pairs and (not isinstance(final, dict) or final.get("board_sha256") != final_sha
                           or final.get("connections") != earlier_pairs)) or isinstance(final, dict) and final.get("problem"):
        return "Earlier named connections were not confirmed on the final saved board"
    final_p = summary.get("final_pours")
    if (earlier_pours and (not isinstance(final_p, dict) or final_p.get("board_sha256") != final_sha
                           or final_p.get("pours") != earlier_pours)) or isinstance(final_p, dict) and final_p.get("problem"):
        return "Earlier named pours were not confirmed on the final saved board"
    if kept[-1]["reviewed_sha256"] != final_sha:
        return "Downloaded PCB differs from the independently reviewed saved board"
    return ""


def route_quality_warning_detail(item: dict) -> str:
    """Canonical geometry line matching editor_step.finding_details for one route-quality finding."""
    point = item.get("point_mm")
    if isinstance(point, tuple):
        point = list(point)
    return (f'route quality: {item.get("severity")} {item.get("rule")} on {item.get("net")}, '
            f'layer {item.get("layer")} at {point}: {item.get("message")}')


def _warning_reasons(answer: str) -> dict[int, str]:
    reasons = {}
    for line in answer.splitlines():
        match = re.fullmatch(r"FINE AS IS: Warning ([1-9][0-9]*):\s*(\S.*)", line.strip())
        if match:
            reasons[int(match[1])] = match[2].strip()
    return reasons


def routing_receipt_dirs(root: pathlib.Path, pcb_rel: str, routing_agent: dict | None = None) -> list[pathlib.Path]:
    """Folders that may hold committed run-*.jsonl routing receipts for one board."""
    root = pathlib.Path(root)
    candidates = [(root / pcb_rel).parent]
    tasks = (routing_agent or {}).get("tasks") if isinstance(routing_agent, dict) else None
    if isinstance(tasks, str) and tasks.strip():
        candidates.append((root / tasks).parent)
    seen, dirs = set(), []
    for folder in candidates:
        resolved = folder.resolve()
        if resolved not in seen:
            seen.add(resolved)
            dirs.append(folder)
    return dirs


def accepted_route_quality_warnings(root: pathlib.Path, pcb_rel: str, search_dirs=()) -> dict[str, dict]:
    """Every route-quality warning geometry explicitly accepted (FINE AS IS) in a verified adopted routing receipt."""
    root = pathlib.Path(root)
    pcb_norm = str(pcb_rel).replace("\\", "/")
    folders = list(search_dirs) if search_dirs else [(root / pcb_norm).parent]
    receipt_paths = []
    seen_files = set()
    skip_parts = {"production", "superseded", ".git"}
    for folder in folders:
        folder = pathlib.Path(folder)
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob("run-*.jsonl")):
            if not path.is_file():
                continue
            resolved = path.resolve()
            if resolved in seen_files:
                continue
            try:
                rel_parts = resolved.relative_to(root.resolve()).parts
            except ValueError:
                rel_parts = path.parts
            if skip_parts & set(rel_parts):
                continue
            seen_files.add(resolved)
            receipt_paths.append(path)

    accepted: dict[str, dict] = {}
    for path in receipt_paths:
        try:
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except (OSError, UnicodeError, ValueError):
            continue
        if len(rows) < 2 or not all(isinstance(row, dict) for row in rows):
            continue
        summary = rows[-1]
        if (summary.get("summary") is not True or summary.get("adopted") is not True
                or str(summary.get("pcb", "")).replace("\\", "/") != pcb_norm):
            continue
        provider = summary.get("provider")
        kept = [step for step in rows[:-1] if step.get("kept") is True]
        if not kept:
            continue
        final_sha = kept[-1].get("reviewed_sha256")
        if adoption_problem(rows[:-1], summary, final_sha, provider):
            continue
        try:
            rel_receipt = path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            rel_receipt = path.as_posix()
        for step in kept:
            proof = step.get("review_receipt") or {}
            warnings = proof.get("warnings") or []
            if not warnings:
                continue
            reasons = _warning_reasons(proof.get("answer", ""))
            counts = collections.Counter(warnings)
            for index, detail in enumerate(warnings, 1):
                prev_count = accepted[detail]["count"] if detail in accepted else 0
                accepted[detail] = {
                    "detail": detail,
                    "receipt": rel_receipt,
                    "task": step["task"],
                    "reviewer": proof["reviewer"],
                    "board_sha256": proof["board_sha256"],
                    "reason": reasons.get(index, ""),
                    "count": max(prev_count, counts[detail]),
                }
    return accepted


def classify_route_quality_warnings(
    quality_report: dict,
    accepted: dict[str, dict],
) -> tuple[list[dict], list[dict], int]:
    """Partition route-quality warnings into (reviewer_accepted, unreviewed_items, unlisted_count)."""
    summary = quality_report.get("summary") if isinstance(quality_report, dict) else None
    reported = summary.get("warnings") if isinstance(summary, dict) else None
    findings = quality_report.get("findings") if isinstance(quality_report, dict) else None
    warning_items = [item for item in (findings or []) if isinstance(item, dict) and item.get("severity") == "warning"]
    if not isinstance(reported, int) or isinstance(reported, bool) or reported < 0:
        return [], warning_items, 1
    if len(warning_items) != reported:
        return [], warning_items, abs(reported - len(warning_items)) or reported
    remaining = {detail: info["count"] for detail, info in accepted.items()}
    matched: list[dict] = []
    unaccepted: list[dict] = []
    for item in warning_items:
        detail = route_quality_warning_detail(item)
        if remaining.get(detail, 0) > 0:
            remaining[detail] -= 1
            info = accepted[detail]
            matched.append({
                "detail": detail,
                "receipt": info["receipt"],
                "task": info["task"],
                "reviewer": info["reviewer"],
                "board_sha256": info["board_sha256"],
                "reason": info["reason"],
            })
        else:
            unaccepted.append(item)
    return matched, unaccepted, 0
