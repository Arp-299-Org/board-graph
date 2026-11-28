#!/usr/bin/env python3
"""Write the data the board graph view reads: the graph, where it stands, the workspace and the log.

`snapshot` writes view.json from tools/board_graph.json and the graph's one database, on the Mac
(board_graph.place(): the file there, over SSH from here), into this checkout's tools/board_graph_view
(or --out): the output of `board_graph.py where --full --json` with its workspace (checkouts here and on
the Mac against GitHub's main, what tidy has left and what main lacks, the pull request a position waits on),
each node's open attempt (who is working it), GitHub's open pull requests each tied to the node whose
record names it, and the log: next's script runs, the cards next started for a person and tidy runs from the
database and the transitions recorded by hand, newest first. The view also names the machine and account names the
page's Hide private takes out (private_names), so the page names no machine. It also writes snapshot.js beside this
checkout's index.html, the copy kept in git that the page loads when it is opened as a file and cannot read
view.json. It records nothing: the graph file and the
track positions stay as they are. Reading the workspace fetches from GitHub here and on the Mac, as
`where --full` does, and keeps that read in the database for the quick `where` and the page the Mac
serves (scripts/board_graph_serve.py), whose view.json the Mac rewrites on that write.

`snapshot --quick` writes view.json alone, from the database, with no git and no network: the graph,
the positions and the log read now, the workspace carried from the newest full read kept in the database
(`where --full` or a full snapshot, on any machine) except the last tidy run and the merge questions, which
the database holds as they are now.
board_graph.py runs it after every write to the state, so an open page follows the graph by itself;
snapshot.js changes only on a full snapshot.

  py -3 scripts/board_graph_view.py snapshot
  py -3 scripts/board_graph_view.py snapshot --quick
"""
from __future__ import annotations

import argparse
import ast
import csv
import functools
import getpass
import hashlib
import io
import inspect
import json
import os
import pathlib
import platform
import re
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import tool_paths  # noqa: E402

if __name__ == "__main__":
    tool_paths.take_argument()

import boards  # noqa: E402

if __name__ == "__main__":
    boards.take_argument()  # --board <name> chooses the board before board_graph reads its description

import graph_pack  # noqa: E402
import board_autonomy  # noqa: E402
import board_heal  # noqa: E402
import mac  # noqa: E402
from board_graph import (ACTIONS, AGENT, CHOSEN, DONE, DROPPED, GRAPH, ONE, PACK, RECORDS, REPORTS, LATER, ROOT, VIEW_IN, Graph, carried_workspace,  # noqa: E402
                         checks,
                         kept_report, naming_record, now, open_pull_requests, open_state, origin_settle_command, parse_time, place,
                         unreached, where, work, workspace)

# The copy kept in git: a script that sets one global, so the page loads it from disk where it cannot fetch.
SNAPSHOT_FILE = "snapshot.js"
SNAPSHOT_START, SNAPSHOT_END = "window.BOARD_GRAPH_SNAPSHOT = ", ";\n"
LOG_LIMIT = 60          # rows in the log, newest first; the database keeps every one
SAID_LIMIT = 6000       # characters of tidy's own words kept per upkeep row


@functools.lru_cache(maxsize=None)
def action_code(path):
    """A card scripts' file's (`path`) top-level functions and classes by name, the methods of its classes by name, and
    the module each top-level imported name comes from."""
    tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
    defs = {node.name: node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
    methods = {}
    for node in defs.values():
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.FunctionDef):
                    methods.setdefault(item.name, []).append(item)
    imported = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module and not node.level:
            imported.update({alias.asname or alias.name: node.module for alias in node.names})
        elif isinstance(node, ast.Import):
            imported.update({alias.asname or alias.name.split(".")[0]: alias.name for alias in node.names})
    return defs, methods, imported


def runs_in_turn(run):
    """The project scripts an action's function runs in turn, read from the code: the scripts/ modules that it,
    and each function, class or method of its own file it reaches by name, import. Empty for a function defined
    anywhere else (a test's stand-in)."""
    path = pathlib.Path(inspect.getsourcefile(run)).resolve()
    defs, methods, imported = action_code(str(path))
    if run.__name__ not in defs or defs[run.__name__].lineno != inspect.getsourcelines(run)[1]:
        return []
    seen, todo, modules = set(), [defs[run.__name__]], set()
    while todo:
        node = todo.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        # A class reached by name runs its __init__; its other methods run when code reaches them by name.
        parts = ([item for item in node.body if isinstance(item, ast.FunctionDef) and item.name == "__init__"]
                 if isinstance(node, ast.ClassDef) else [node])
        for part in parts:
            for sub in ast.walk(part):
                if isinstance(sub, ast.Import):
                    modules.update(alias.name for alias in sub.names)
                elif isinstance(sub, ast.ImportFrom) and sub.module and not sub.level:
                    modules.add(sub.module)
                elif isinstance(sub, ast.Name) and sub.id in defs:
                    todo.append(defs[sub.id])
                elif isinstance(sub, ast.Name) and sub.id in imported:
                    modules.add(imported[sub.id])
                elif isinstance(sub, ast.Attribute) and sub.attr in methods:
                    todo.extend(methods[sub.attr])
    return sorted(f"scripts/{name}.py" for name in modules if (path.parent / f"{name}.py").is_file())


def what_runs(graph, state, key, root=ROOT, actions=None):
    """What actually runs at node `key`, for the node's panel, or None when the node names no script.

    The action's name; whether next runs it (connected) or it waits for KiCad or the Mac (LATER); for a connected
    one, its function, file and line, the first paragraph of its docstring, the scripts it runs in turn and the
    paths it reads; and its last run: when, by what, the outcome, the findings and the report.
    """
    node = graph.nodes[key]
    action = node["action"] or node.get("pass_check")
    if not action:
        return None
    actions = ACTIONS if actions is None else actions
    item = {"action": action, "connected": action in actions, "later": action in LATER}
    if action in actions:
        if not node["action"]:
            item["when"] = "pass"
        run, reads = actions[action]
        source = pathlib.Path(inspect.getsourcefile(run)).resolve()
        for base in (pathlib.Path(ROOT).resolve(), tool_paths.TOOL_ROOT.resolve()):
            try:
                source = source.relative_to(base).as_posix()
                break
            except ValueError:
                continue
        else:
            source = str(source)
        doc = " ".join((inspect.getdoc(run) or "").split("\n\n")[0].split())
        # A constant the docstring names shows its value: "within LIBRARY_HEIGHT_MM (0.3)".
        module = sys.modules.get(run.__module__)
        doc = re.sub(r"\b[A-Z][A-Z0-9_]{2,}\b", lambda m: (
            f"{m.group(0)} ({getattr(module, m.group(0))})"
            if isinstance(getattr(module, m.group(0), None), (int, float)) else m.group(0)), doc)
        item.update(function=run.__name__, source=source, line=inspect.getsourcelines(run)[1], doc=doc,
                    uses=runs_in_turn(run), reads=list(reads))
    ran = [attempt for attempt in state.attempts(key) if attempt["action"] == action]
    if ran:
        last = ran[-1]
        item["last"] = {"run_id": last["run_id"], "started_at": last["started_at"],
                        "finished_at": last["finished_at"], "outcome": last["outcome"], "by": last["actor"],
                        "report": last["report_path"], "report_read": report_read(last, root)}
    return item


def node_states(graph, state, root=ROOT):
    """Per track: position, done and started. Per node: passed, current, ahead (or open, off a row); its open
    attempt: who is on it now (a script next runs, or a person next started it for), the newest when there are two;
    and what runs there (what_runs)."""
    tracks, nodes = {}, {}
    held = {}
    for attempt in state.attempts(open_only=True):
        held[attempt["node"]] = work(attempt)
    for track in graph.data["tracks"]:
        keys = graph.track_nodes(track["id"])
        started = bool(state.transitions(track["id"]))
        if track["ordered"]:
            position = state.position(graph, track["id"])
            reached = len(keys) if position == DONE else keys.index(position)
            rows = state.transitions(track["id"])
            # A track the owner dropped is done with the cards from the one it was dropped at still ahead.
            dropped = position == DONE and bool(rows) and rows[-1]["outcome"] == DROPPED and rows[-1]["node"] in keys
            if dropped:
                reached = keys.index(rows[-1]["node"]) - 0.5
            tracks[track["id"]] = {"position": position, "done": position == DONE, "started": started}
            if dropped:
                tracks[track["id"]]["dropped"] = True
        else:
            reached = None
            tracks[track["id"]] = {"position": None, "done": False, "started": started}
        for index, key in enumerate(keys):
            history = state.transitions(node=key)
            passed = any(row["outcome"] == "pass" for row in history)
            if reached is None:
                status = "passed" if passed else "open"
            else:
                status = "passed" if index < reached else "current" if index == reached else "ahead"
            last = history[-1] if history else None
            nodes[key] = {
                "status": status,
                # A node past the position that passed before: the work went back behind it.
                "passed_before": status == "ahead" and passed,
                "transitions": len(history),
                "last": {k: last[k] for k in ("at", "outcome", "target", "actor", "note")} if last else None,
                "attempt": held.get(key),
                "runs": what_runs(graph, state, key, root),
            }
    return tracks, nodes


def when(text):
    """A row's time for sorting; a time that does not parse sorts first."""
    try:
        return parse_time(text)
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)


def report_read(attempt, root):
    """The parts of an attempt's report the log shows (kept_report): the ones its row keeps, else read from its
    file in the checkout the run was in or in `root` (a row from before the parts were kept), else None."""
    if attempt["report"] is not None:
        return attempt["report"]
    if not attempt["report_path"]:
        return None
    for base in [part for part in (attempt["checkout"], root) if part]:
        try:
            return kept_report(json.loads((pathlib.Path(base) / attempt["report_path"]).read_text(encoding="utf-8")))
        except (OSError, TypeError, ValueError):
            continue
    return None


def log(state, root=ROOT, limit=LOG_LIMIT, graph=None):
    """What ran and what was recorded, newest first, at most `limit` rows.

    attempt: a script next ran, or its merge check, with the record its report produced (the transition
    that cites the report as evidence). work: a person or an agent on a card, started with `next` and
    closed by the node's next record (its outcome) or by start --take. upkeep: a tidy run next made, with why,
    the outcome and tidy's own words. record: a transition no attempt produced, recorded by a person or an
    agent with `record`.
    """
    transitions = state.transitions()
    cited = {}
    for row in transitions:
        for item in row["evidence"]:
            cited.setdefault(item["path"], row)
    rows, produced = [], set()
    for attempt in state.attempts():
        if attempt["action"] == AGENT:
            launch = attempt.get("launch_state")
            unconfirmed = (attempt.get("report") or {}).get("termination_unconfirmed", False)
            warning = (" Verify the worker and every descendant stopped first. " if unconfirmed else "")
            verification = " --verified-stopped" if unconfirmed else ""
            command = (origin_settle_command(graph, state, attempt["node"], root, attempt["run_id"], ack=True,
                                              unconfirmed=unconfirmed) if graph is not None else
                       f"py -3 scripts/board_graph.py{CHOSEN.argument} settle {attempt['node']} --attempt {attempt['run_id']} "
                       f"--ack{verification} --by agent:<lead> --note \"<revised approach>\"")
            prefix = (f"Launch {attempt['run_id']} needs the lead: {attempt.get('launch_note')}. "
                      f"{warning}"
                      f"Acknowledge on the machine serving this view: {command}. "
                      if launch == "needs_lead" else
                      f"Launch {attempt['run_id']} still owns the work. " if launch == "active" else "")
            if launch == "acknowledged":
                ack = (attempt.get("report") or {}).get("acknowledged", {})
                prefix = f"Launch checkpoint acknowledged by {ack.get('by')}: {ack.get('note')}. "
            rows.append({
                "kind": "work", "id": attempt["run_id"], "at": attempt["started_at"],
                "finished_at": attempt["finished_at"], "node": attempt["node"], "what": "work",
                "outcome": attempt["outcome"], "record": None, "target": None, "actor": attempt["actor"],
                "note": prefix + attempt["note"], "launch_state": launch, "launch_note": attempt.get("launch_note"),
            })
            continue
        made = cited.get(attempt["report_path"]) if attempt["report_path"] else None
        if made:
            produced.add(made["seq"])
        rows.append({
            "kind": "attempt", "id": attempt["run_id"], "at": attempt["started_at"],
            "finished_at": attempt["finished_at"], "node": attempt["node"], "what": attempt["action"],
            "outcome": attempt["outcome"], "report": attempt["report_path"],
            "report_read": report_read(attempt, root),
            "record": made["record_name"] if made else None, "target": made["target"] if made else None,
            "actor": made["actor"] if made else None, "note": made["note"] if made else None,
        })
    for upkeep in state.upkeeps():
        said = upkeep["said"] or ""
        rows.append({
            "kind": "upkeep", "id": upkeep["run_id"], "at": upkeep["started_at"],
            "finished_at": upkeep["finished_at"], "node": upkeep["node"], "what": upkeep["why"],
            "outcome": upkeep["outcome"], "record": None, "mac_skipped": upkeep["mac"],
            "problems": upkeep["problems"], "said": said[:SAID_LIMIT], "said_cut": len(said) > SAID_LIMIT,
        })
    for row in transitions:
        if row["seq"] in produced:
            continue
        rows.append({
            "kind": "record", "id": row["record_name"] or f"transition-{row['seq']}", "at": row["at"],
            "finished_at": None, "node": row["node"], "what": "record", "outcome": row["outcome"],
            "record": row["record_name"], "target": row["target"], "actor": row["actor"], "note": row["note"],
            "evidence": [item["path"] for item in row["evidence"]],
        })
    rows.sort(key=lambda item: when(item["at"]), reverse=True)
    counts = {kind: sum(1 for item in rows if item["kind"] == kind) for kind in ("attempt", "work", "upkeep", "record")}
    return {"rows": rows[:limit], "total": len(rows), "limit": limit, "counts": counts}


def workspace_section(graph, state, root=ROOT, read_workspace=None, list_pull_requests=None):
    """`where`'s full read of the workspace (board_graph.workspace: the checkouts, the Mac, the merge questions
    and GitHub's open pull requests each tied to the node whose record names it), kept in the database as it
    is read. A workspace that cannot be read is an error in the section, never a failed snapshot: the graph still
    shows."""
    try:
        if read_workspace:
            return read_workspace(graph, state, root)
        return workspace(graph, state, root, list_pull_requests=list_pull_requests)
    except Exception as error:  # noqa: BLE001 - whatever stops it, the page says so and the graph still shows
        return {"at": now(), "error": f"{type(error).__name__}: {error}"}


def project_name(root=ROOT):
    """The project's name from its board description, for the page's title; None when there is no description."""
    import board_description
    given = None if os.environ.get(board_description.ENV) else CHOSEN.description
    try:
        return board_description.load(given, root=root)["project"]
    except board_description.DescriptionError:
        pass
    # A graph that is not a board (the pack "none") may still have a small file that names its project.
    try:
        named = json.loads(board_description.find(given, root=root).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    project = named.get("project") if isinstance(named, dict) else None
    return project if isinstance(project, str) and project else None


# The user name in a home folder's path: C:\Users\<name>\..., /Users/<name>/..., /home/<name>/...
HOME_USER = re.compile(r"(?:^|[\\/])(?:Users|home)[\\/]([^\\/\s\"']+)", re.IGNORECASE)


def _name_and_short(name):
    """A machine's name and, for a dotted one (host.local), its first part."""
    name = (name or "").strip()
    return [name, name.split(".")[0]] if name else []


def private_names(section, root=ROOT, user=None, host=None):
    """The machine and account names the page's Hide private takes out, so the page itself names no machine: the
    site settings' SSH user and host, this machine's user and host name, and from the workspace `section`, the
    machine that read it and the users whose home folders its checkouts are in. Sorted, each once."""
    import site_settings
    try:
        ssh = site_settings.load(root).ssh_host
    except site_settings.SiteError:
        ssh = ""
    login, _, machine = ssh.rpartition("@")
    names = [login] + _name_and_short(machine)
    if user is None:
        try:
            user = getpass.getuser()
        except Exception:  # noqa: BLE001 - no login name to be had on this machine: nothing to add
            user = ""
    names += [user] + _name_and_short(platform.node() if host is None else host)
    if isinstance(section, dict):
        names += _name_and_short(section.get("read_on"))
        for item in (section.get("here") or {}).get("checkouts") or []:
            match = HOME_USER.search(str(item.get("path") or "")) if isinstance(item, dict) else None
            if match:
                names.append(match.group(1))
    return sorted({name for name in names if name})


def listed_boards(root=ROOT):
    """What the page's boards row is drawn from: each listed board's name and the folder its view is in, relative
    to the page ("" for the first board, boards/<name>/ for another). Empty in a repository with one board, and
    when the list cannot be read: the graph still shows."""
    try:
        listed = boards.read(root)
    except boards.BoardsError:
        return []
    if len(listed) < 2:
        return []
    found = []
    for board in listed.values():
        item = {"name": board.name, "data": board.data}
        if PACK.name != graph_pack.ELECTRONICS:
            # A graph that is not a board is listed by the name its description gives it, when it has one.
            try:
                title = json.loads((pathlib.Path(root) / board.description).read_text(encoding="utf-8-sig")).get("name")
            except (OSError, ValueError, AttributeError):
                title = None
            if isinstance(title, str) and title and title != board.name:
                item["title"] = title
        found.append(item)
    return found


HUMAN_FILE_LABELS = {
    "3d_top": "3D top view",
    "3d_bottom": "3D bottom view",
    "front_silk": "Front silkscreen",
    "front_silk_and_mask": "Front silkscreen & mask",
    "back_silk": "Back silkscreen",
    "back_silk_and_mask": "Back silkscreen & mask",
    "front_copper": "Front copper",
    "back_copper": "Back copper",
    "front_copper_only": "Front copper only",
    "back_copper_only": "Back copper only",
    "front_no_pour": "Front copper (no pour)",
    "back_no_pour": "Back copper (no pour)",
    "all_copper_stack": "All copper layers",
    "in1_copper": "Inner copper 1",
    "in2_copper": "Inner copper 2",
    "erc.rpt": "ERC report",
    "drc.rpt": "DRC report",
    "drc-parity.rpt": "Schematic-to-board parity report",
    "listings.md": "Part stock & pricing",
    "manual_joints.md": "Hand-soldered joints check",
    "electrical_audit_contract.md": "Electrical rules & contract",
    "order_package_contract.md": "Order package contract",
    "board_order_common_sense_checklist.md": "Pre-order checklist",
    "space.md": "Board space & dimensions",
    "board_decisions.md": "Board decisions",
    "decisions.md": "Board decisions",
    "product_truth.md": "Product truth",
    "body_hardware.md": "Body hardware figures",
    "routing_notes.md": "Routing notes",
}
VIEWABLE_REPORT_EXTS = (".pdf", ".png", ".jpg", ".jpeg", ".webp", ".svg", ".md", ".csv", ".txt", ".rpt", ".zip")
DATASHEET_EXTS = (".pdf", ".png", ".jpg", ".jpeg", ".webp", ".svg")


def _safe_rel(rel):
    """Normalized repo-relative forward-slash path, or None if hidden/outside."""
    if not rel or not isinstance(rel, str):
        return None
    norm = pathlib.PurePosixPath(rel.replace("\\", "/")).as_posix().lstrip("/")
    if not norm or norm == ".":
        return None
    parts = [part for part in norm.split("/") if part]
    if any(part in (".", "..", "__pycache__") or part.startswith(".") for part in parts):
        return None
    if parts[0] == "logs":
        return None
    return "/".join(parts)


def _kind_for_path(rel, is_dir=False):
    if is_dir:
        return "folder"
    ext = pathlib.PurePosixPath(rel).suffix.lower()
    return {
        ".pdf": "pdf",
        ".png": "image",
        ".jpg": "image",
        ".jpeg": "image",
        ".webp": "image",
        ".svg": "image",
        ".csv": "csv",
        ".md": "doc",
        ".json": "json",
        ".rpt": "report",
        ".txt": "report",
        ".step": "model",
        ".stp": "model",
        ".zip": "archive",
        ".kicad_sch": "kicad",
        ".kicad_pcb": "kicad",
        ".kicad_mod": "footprint",
    }.get(ext, "file")


def _human_label(rel, stem="", card=""):
    name = pathlib.PurePosixPath(rel).name
    lower = name.lower()
    if lower in HUMAN_FILE_LABELS:
        return HUMAN_FILE_LABELS[lower]
    base = pathlib.PurePosixPath(name).stem
    for prefix in (f"{stem}-{card}-", f"{stem}_", f"{stem}-", f"{card}-", f"{card}_"):
        if prefix and base.lower().startswith(prefix.lower()):
            base = base[len(prefix):]
    if base.lower() in HUMAN_FILE_LABELS:
        return HUMAN_FILE_LABELS[base.lower()]
    cleaned = base.replace("_", " ").replace("-", " ").strip()
    return (cleaned[:1].upper() + cleaned[1:]) if cleaned else name


def _artifact_item(root_path, rel, label=None, kind=None, primary=False, expect_dir=False, stem="", card=""):
    clean = _safe_rel(rel)
    if not clean:
        return None
    target = root_path / clean
    exists = target.exists()
    is_dir = target.is_dir() if exists else bool(expect_dir)
    item = {
        "path": clean,
        "label": label or _human_label(clean, stem=stem, card=card),
        "exists": exists,
        "is_dir": is_dir,
        "kind": kind or _kind_for_path(clean, is_dir=is_dir),
        "primary": bool(primary),
    }
    if exists and is_dir:
        try:
            item["count"] = sum(1 for sub in target.iterdir() if not sub.name.startswith(".") and sub.name != "__pycache__")
        except OSError:
            item["count"] = 0
    elif exists:
        try:
            item["size"] = target.stat().st_size
        except OSError:
            pass
    return item


def _bom_summary(root_path, bom_rel):
    clean = _safe_rel(bom_rel)
    if not clean:
        return None
    path = root_path / clean
    if not path.is_file():
        return None
    try:
        reader = csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig")))
        rows = []
        total_qty = 0
        for row in reader:
            qty_raw = (row.get("Qty") or row.get("Quantity") or "0").strip()
            try:
                total_qty += int(qty_raw)
            except ValueError:
                pass
            rows.append({
                "refs": (row.get("Refs") or row.get("Reference") or row.get("Designator") or "").strip(),
                "qty": qty_raw,
                "value": (row.get("Value") or row.get("Comment") or "").strip(),
                "maker": (row.get("Manufacturer") or "").strip(),
                "mpn": (row.get("MPN") or row.get("Part") or "").strip(),
                "supplier": (row.get("Supplier") or "").strip(),
                "supplier_part": (row.get("Supplier Part") or row.get("LCSC Part #") or "").strip(),
            })
        return {"path": clean, "unique_parts": len(rows), "total_qty": total_qty, "rows": rows[:80]}
    except (OSError, ValueError, csv.Error):
        return None


def _datasheets_summary(root_path, datasheets_rel, parts_rel, bom):
    ds_clean = _safe_rel(datasheets_rel)
    if not ds_clean:
        return None
    ds_dir = root_path / ds_clean
    files = []
    if ds_dir.is_dir():
        for item in sorted(ds_dir.iterdir(), key=lambda p: p.name.lower()):
            if item.is_file() and not item.name.startswith(".") and item.suffix.lower() in DATASHEET_EXTS:
                try:
                    size = item.stat().st_size
                except OSError:
                    size = 0
                files.append({
                    "name": item.name,
                    "path": f"{ds_clean}/{item.name}",
                    "size": size,
                    "kind": _kind_for_path(item.name),
                })
    parts_map = {}
    parts_clean = _safe_rel(parts_rel)
    if parts_clean and (root_path / parts_clean).is_file():
        try:
            raw_parts = json.loads((root_path / parts_clean).read_text(encoding="utf-8-sig"))
            if isinstance(raw_parts, dict) and isinstance(raw_parts.get("parts"), dict):
                parts_map = raw_parts["parts"]
        except (OSError, ValueError):
            parts_map = {}
    board_parts = []
    if bom and bom.get("rows") and parts_map:
        for row in bom["rows"]:
            mpn = row.get("mpn") or ""
            info = parts_map.get(mpn) or {}
            ds_name = info.get("datasheet")
            ds_path = f"{ds_clean}/{ds_name}" if ds_name else None
            ds_exists = bool(ds_path and (root_path / ds_path).is_file())
            board_parts.append({
                "refs": row.get("refs", ""),
                "value": row.get("value", ""),
                "maker": info.get("maker") or row.get("maker", ""),
                "mpn": mpn,
                "package": info.get("package", ""),
                "height_mm": info.get("height_mm"),
                "path": ds_path,
                "exists": ds_exists,
            })
    return {"folder": ds_clean, "exists": ds_dir.is_dir(), "board_parts": board_parts, "files": files}


def artifacts(graph, state, root=ROOT):
    """Each card's files, read from disk: for a board, its files, exports, renders and datasheets, from the board
    description; for a graph that is not a board, the card's reports, the files it owns and the docs it names.

    Empty when a board's `root` holds no readable board description (such as a synthetic unit-test directory).
    """
    import board_description
    root_path = pathlib.Path(root)
    given = None if os.environ.get(board_description.ENV) else CHOSEN.description
    try:
        desc = board_description.load(given, root=root_path)
    except (board_description.DescriptionError, OSError, ValueError):
        if PACK.name == graph_pack.ELECTRONICS:
            return {}
        desc = None  # a graph that is not a board: every card shows its reports, its own files and its docs
    stem = desc.stem if desc else ""
    folder = desc.rel("folder") if desc else None
    reports_rel = desc.rel("reports") if desc else REPORTS
    bom = _bom_summary(root_path, desc.rel("bom_export")) if desc else None
    datasheets = (_datasheets_summary(root_path, desc.rel("datasheets"), desc.rel("parts_data"), bom) if desc
                  else None)

    transitions_by_node = {}
    for row in state.transitions():
        transitions_by_node[row["node"]] = row

    cards = {}
    for node_id, node in graph.nodes.items():
        items = []
        seen = set()
        badge = None

        def add(rel, label=None, kind=None, primary=False, expect_dir=False, only_if_exists=False):
            entry = _artifact_item(root_path, rel, label=label, kind=kind, primary=primary,
                                   expect_dir=expect_dir, stem=stem, card=node_id)
            if not entry or entry["path"] in seen:
                return None
            if only_if_exists and not entry["exists"]:
                return None
            seen.add(entry["path"])
            items.append(entry)
            return entry

        def add_folder_files(dir_rel, exts=VIEWABLE_REPORT_EXTS, primary_first=False):
            clean_dir = _safe_rel(dir_rel)
            base_dir = root_path / clean_dir if clean_dir else None
            if not base_dir or not base_dir.is_dir():
                return 0
            entries = list(base_dir.iterdir()) + [sub for d in base_dir.iterdir() if d.is_dir() and not d.name.startswith(".") for sub in d.iterdir()]
            found = 0
            children = sorted((p for p in entries if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in exts),
                              key=lambda p: (p.parent != base_dir, p.suffix.lower() not in (".pdf", ".png", ".jpg", ".zip", ".md", ".csv"), p.relative_to(base_dir).as_posix().lower()))
            for child in children:
                add(f"{clean_dir}/{child.relative_to(base_dir).as_posix()}", primary=(primary_first and found == 0))
                found += 1
            return found

        if desc is None:
            count = add_folder_files(f"{reports_rel}/{node_id}", primary_first=True)
            if count:
                badge = f"{count} {'file' if count == 1 else 'files'}"
            for owned in node.get("owns", []):
                add(owned, primary=not items, only_if_exists=True)
            for doc in node.get("docs", []):
                add(doc, primary=not items, only_if_exists=True)
        elif node_id == "inventory":
            add(desc.rel("schematic_export"), "Schematic (PDF)", "pdf", primary=True, only_if_exists=True)
            add(desc.rel("bom_export"), "Bill of materials (CSV)", "csv", only_if_exists=True)
            add(desc.rel("schematic"), "KiCad schematic (.kicad_sch)", "kicad")
            add(desc.rel("pcb"), "KiCad PCB (.kicad_pcb)", "kicad", only_if_exists=True)
        elif node_id == "decisions":
            add(desc.rel("decisions"), "Board decisions", "doc", primary=True)
            for doc in graph.data.get("docs", []):
                add(doc, primary=False, only_if_exists=True)
        elif node_id == "constraints":
            for owned in node.get("owns", []):
                add(owned, primary=not items)
            add(f"{folder}/SPACE.md", "Board space & dimensions", "doc", only_if_exists=True)
            for doc in node.get("docs", []):
                add(doc, only_if_exists=True)
        elif node_id == "schematic":
            sch_pdf = add(desc.rel("schematic_export"), "Schematic (PDF)", "pdf", primary=True)
            if sch_pdf and sch_pdf["exists"]:
                badge = "PDF ready"
            add_folder_files(f"{reports_rel}/schematic")
            add(desc.rel("schematic"), "KiCad schematic (.kicad_sch)", "kicad", only_if_exists=True)
        elif node_id == "bom":
            bom_item = add(desc.rel("bom_export"), "Bill of materials (CSV)", "csv", primary=True)
            if bom and bom.get("unique_parts"):
                n = bom["unique_parts"]
                badge = f"{n} {'part' if n == 1 else 'parts'}"
            elif bom_item and bom_item["exists"]:
                badge = "CSV ready"
        elif node_id == "stock":
            count = add_folder_files(f"{reports_rel}/stock", primary_first=True)
            if count:
                badge = "Listed"
            add(desc.rel("bom_export"), "Bill of materials (CSV)", "csv", only_if_exists=True)
        elif node_id == "datasheets":
            ds_item = add(desc.rel("datasheets"), "Datasheets folder", "folder", primary=True, expect_dir=True)
            if datasheets and datasheets["files"]:
                n = len(datasheets["files"])
                badge = f"{n} {'file' if n == 1 else 'files'}"
            elif ds_item and ds_item.get("count"):
                badge = f"{ds_item['count']} files"
        elif node_id == "models":
            model_count = 0
            for mfolder in desc.rels("model_folders"):
                add(mfolder, "3D models folder", "folder", primary=(model_count == 0), expect_dir=True)
                model_count += add_folder_files(mfolder, exts=(".step", ".stp", ".wrl"))
            if model_count:
                badge = f"{model_count} {'model' if model_count == 1 else 'models'}"
        elif node_id == "footprints":
            fp = add(desc.rel("footprint_library"), "Footprint library (.pretty)", "folder", primary=True, expect_dir=True)
            if fp and fp.get("count"):
                n = fp["count"]
                badge = f"{n} {'footprint' if n == 1 else 'footprints'}"
            add_folder_files(f"{reports_rel}/footprints")
        elif node_id in ("placement", "routing_batch", "visual_review", "orientation_preview"):
            renders = add_folder_files(f"{reports_rel}/{node_id}",
                                       exts=(".png", ".jpg", ".jpeg", ".webp", ".pdf", ".md"),
                                       primary_first=True)
            if renders:
                badge = f"{renders} {'view' if renders == 1 else 'views'}"
            if node_id == "placement":
                add(f"{folder}/SPACE.md", "Board space & dimensions", "doc", only_if_exists=True)
            if node_id == "orientation_preview":
                add(desc.rel("decisions"), "Board decisions", "doc", only_if_exists=True)
        elif node_id == "package":
            prod_files = add_folder_files(desc.rel("production"), primary_first=True)
            add(desc.rel("production"), "Production package folder", "folder",
                primary=(prod_files == 0), expect_dir=True)
            if prod_files:
                badge = f"{prod_files} {'file' if prod_files == 1 else 'files'}"
        else:
            count = add_folder_files(f"{reports_rel}/{node_id}", primary_first=True)
            if count:
                badge = f"{count} {'file' if count == 1 else 'files'}"
            for owned in node.get("owns", []):
                add(owned, primary=not items, only_if_exists=True)
            for doc in node.get("docs", []):
                add(doc, primary=not items, only_if_exists=True)

        last_row = transitions_by_node.get(node_id)
        if last_row:
            for ev in last_row.get("evidence", []):
                ev_path = ev.get("path") if isinstance(ev, dict) else None
                if ev_path and pathlib.PurePosixPath(ev_path).suffix.lower() in VIEWABLE_REPORT_EXTS:
                    add(ev_path, only_if_exists=True)

        cards[node_id] = {"badge": badge, "items": items}

    return {"cards": cards, "bom": bom, "datasheets": datasheets}


def run_timing(graph, state):
    """Earliest run timestamp and completion timestamp from full SQLite history (never lost to view window caps)."""
    board_tr = state.transitions("board")
    ats = [r["at"] for r in board_tr if r.get("at")] + [a["started_at"] for a in state.attempts() if a.get("started_at")]
    started_at = min(ats) if ats else None
    until = (board_autonomy.summary(graph) or {}).get("until")
    finished_at = None
    if board_tr:
        last = board_tr[-1]
        if state.position(graph, "board") is None or (until and last.get("node") == until and last.get("outcome") == "pass"):
            finished_at = last.get("at")
    return {"started_at": started_at, "finished_at": finished_at, "running": bool(started_at and not finished_at)}


def build(graph, state, section, root=ROOT):
    """The view: the graph, where it stands, each node's state, the workspace `section` and the log; with several
    boards, the name of the board it is the view of and the boards there are."""
    tracks, nodes = node_states(graph, state, root)
    graph_path = pathlib.Path(graph.path).resolve()
    return {
        **graph_notes(root),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "project": project_name(root),
        "one_machine": mac.ONE_MACHINE,  # no KiCad machine is named: the page reports no second machine
        "board": CHOSEN.name,
        "boards": listed_boards(root),
        "source": {
            "graph": graph_path.relative_to(ROOT).as_posix() if graph_path.is_relative_to(ROOT) else str(graph.path),
            "graph_sha256": hashlib.sha256(graph_path.read_bytes()).hexdigest(),
        },
        "tool": tool_paths.tool_status(),
        "tool_heal": board_heal.tool_heal_summary(),
        "model": graph.data.get("model") or (graph.data.get("autonomy") or {}).get("model") or os.environ.get("BOARD_GRAPH_MODEL") or None,
        "timing": run_timing(graph, state),
        "graph": graph.data,
        "autonomy": board_autonomy.summary(graph),
        "where": where(graph, state),
        "state": {"tracks": tracks, "nodes": nodes},
        "artifacts": artifacts(graph, state, root),
        "workspace": section,
        "log": log(state, root, graph=graph),
        "private_names": private_names(section, root),
    }


NOTES_CHARS = 20000  # as much of a graph's notes as the view carries: the page reads the file itself when served


def graph_notes(root=ROOT):
    """{"notes": {path, exists, text}} for a graph whose pack names a notes file, the running notes the page shows
    beside the cards; {} otherwise. The text is the file's first NOTES_CHARS characters, for a page opened from
    disk: a served page reads the file itself, so it shows what was written since."""
    path = PACK.notes_path(CHOSEN.name)
    if not path:
        return {}
    try:
        text = (pathlib.Path(root) / path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"notes": {"path": path, "exists": False, "text": ""}}
    return {"notes": {"path": path, "exists": True, "text": text[:NOTES_CHARS]}}


def write_view(view, out, tell=None):
    """view.json in folder `out`, replaced whole so a page reading it never gets half a file, then the page server
    is told (`tell`: by default the Mac's own, scripts/board_graph_serve.py's notify(), on the Mac only; elsewhere
    there is no server to tell). A server that could not be told is one line on stderr, never a failure."""
    out = pathlib.Path(out)
    out.mkdir(parents=True, exist_ok=True)
    data, part = out / "view.json", out / f"view.json.{os.getpid()}.tmp"
    # Bytes, so the file has LF on every machine (write_text's newline= needs Python 3.10; the Mac runs 3.9).
    part.write_bytes((json.dumps(view, indent=2) + "\n").encode("utf-8"))
    for _ in range(40):
        try:
            os.replace(str(part), str(data))
            break
        except PermissionError:  # Windows: the page's server holds view.json open for a moment
            time.sleep(0.025)
    else:
        data.write_bytes(part.read_bytes())
        part.unlink()
    told(out, tell)
    return data


def told(out, tell=None):
    """Tell the page server that folder `out` changed, so every open page reads it again: None, or why the server
    was not told (also one line on stderr). With no `tell`, the Mac's own server is told, and only when `out` is
    the folder it serves (the main checkout's): a view written anywhere else, a test's temporary folder or
    another machine's checkout, is read by no page, and nothing is said."""
    if tell is None:
        if not mac.LOCAL:
            return None
        served = pathlib.Path(mac.CHECKOUT).expanduser() / VIEW_IN  # the Mac's main checkout's page folder
        if pathlib.Path(out).resolve() != served.resolve():
            return None
        import board_graph_serve
        tell = board_graph_serve.notify
    why = tell()
    if why and mac.ONE_MACHINE and why.startswith("no ("):
        return why  # on a project's one machine the page is served only while someone has it open
    if why:
        sys.stderr.write(f"the page was not told that {out} changed ({why})\n")
    return why


def quick_snapshot(graph, state, out, root=ROOT):
    """The quick snapshot: view.json alone, from the database: the graph, the positions and the log read now, the
    workspace carried from the newest full read a machine kept there (carried_workspace). No git, no network, no
    snapshot.js. Returns the view and the file it wrote."""
    view = build(graph, state, carried_workspace(graph, state, state.last_full_read()), root)
    return view, write_view(view, out)


def snapshot(graph_path=GRAPH, state_path=None, records=None, out=None, root=ROOT, read_workspace=None,
             list_pull_requests=None, quick=False):
    """A full snapshot: view.json with the workspace read over the network, and snapshot.js, the copy the page
    loads when it is opened as a file. With `quick`, the quick snapshot instead: view.json alone, and snapshot.js
    is left as it is. Returns the view, view.json's path and snapshot.js's path (None on a quick one).

    By default the graph's one database (board_graph.place(): on the Mac, the file; elsewhere, over SSH), view.json
    in this checkout's tools/board_graph_view (on the Mac, its main checkout's, the folder the page is served
    from) and snapshot.js in this checkout's tools/board_graph_view, which is in git and committed from this
    checkout's branch; `out` puts both in that folder. Raises mac.MacError when the Mac does not answer.
    """
    where_from = place(state_path, records, out, root)
    graph = Graph(graph_path)
    state = open_state(where_from, records or RECORDS)
    folder = pathlib.Path(out) if out else where_from["view"] or pathlib.Path(root) / VIEW_IN
    if quick:
        view, data = quick_snapshot(graph, state, folder, root)
        return view, data, None
    view = build(graph, state, workspace_section(graph, state, root, read_workspace, list_pull_requests), root)
    data = write_view(view, folder)

    copy = (pathlib.Path(out) if out else pathlib.Path(root) / VIEW_IN) / SNAPSHOT_FILE
    copy.parent.mkdir(parents=True, exist_ok=True)
    # Bytes, so the file has LF on every machine; < keeps "</script>" in the JSON from reading as a tag.
    text = json.dumps(view, separators=(",", ":")).replace("<", "\\u003c")
    copy.write_bytes(f"{SNAPSHOT_START}{text}{SNAPSHOT_END}".encode("utf-8"))
    return view, data, copy


def read_copy(path):
    """The view a snapshot.js holds, as the page reads it."""
    text = pathlib.Path(path).read_text(encoding="utf-8")
    if not (text.startswith(SNAPSHOT_START) and text.endswith(SNAPSHOT_END)):
        raise ValueError(f"{path} is not a board graph snapshot")
    return json.loads(text[len(SNAPSHOT_START):-len(SNAPSHOT_END)])


def every_board(argv, names):
    """`snapshot` in a repository with several boards and none named: the same command once per board, each in a
    process of its own, its output after a line naming the board. The exit code is the worst one."""
    import subprocess
    from no_window import KWARGS as NO_WINDOW_KWARGS
    worst = 0
    for name in names:
        done = subprocess.run([sys.executable, str(pathlib.Path(__file__).resolve()), boards.ARGUMENT, name, *argv],
                              capture_output=True, **NO_WINDOW_KWARGS)
        worst = max(worst, done.returncode)
        sys.stderr.write(done.stderr.decode("utf-8", "replace").replace("\r\n", "\n"))
        sys.stdout.write(f"{ONE.capitalize()} {name}:\n" + done.stdout.decode("utf-8", "replace").replace("\r\n", "\n"))
    return worst


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    boards.add_argument(parser)
    parser.add_argument("--graph", type=pathlib.Path, default=GRAPH)
    parser.add_argument("--state", type=pathlib.Path,
                        help="another database than the one on the Mac, as for board_graph.py")
    parser.add_argument("--records", type=pathlib.Path, help="the record folder to import from, as for board_graph.py")
    parser.add_argument("--out", type=pathlib.Path,
                        help="the folder for view.json and snapshot.js; by default this checkout's "
                             "tools/board_graph_view (on the Mac, its main checkout's, where the page is served from)")
    commands = parser.add_subparsers(dest="command", required=True)
    write = commands.add_parser("snapshot", help="write view.json and snapshot.js, the copy the page opens from disk")
    write.add_argument("--quick", action="store_true",
                       help="view.json alone, from the database with no git and no network: the workspace is "
                            "carried from the newest full read in the database, and snapshot.js stays as it is")
    write.add_argument(boards.ARGUMENT, metavar="NAME", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.board and args.board != CHOSEN.name:
        print(f"--board {args.board}: this process already works on {CHOSEN.name or CHOSEN.description}; run "
              f"scripts/board_graph_view.py as a program", file=sys.stderr)
        return 2
    try:
        several = boards.unnamed(ROOT)
    except boards.BoardsError as error:
        print(error, file=sys.stderr)
        return 1
    if several:
        # Several boards and none named: every board's view, each written by a process of its own.
        if any(word.split("=", 1)[0] in ("--graph", "--state", "--records", "--out") for word in argv):
            print(f"snapshot: this repository has {len(several)} boards ({', '.join(several)}); name the one these "
                  f"files are for with {boards.ARGUMENT}", file=sys.stderr)
            return 2
        return every_board(argv, several)
    try:
        view, data, page = snapshot(args.graph, args.state, args.records, args.out, quick=args.quick)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    except mac.MacError as error:
        print(unreached(place(args.state, args.records, args.out), error, "snapshot"), file=sys.stderr)
        return 1
    where_now = [f"{t['label']}: {t['position']}" for t in view["where"]["tracks"] if t["ordered"]]
    print(f"wrote {data} at {view['generated_at']}")
    print("quick: snapshot.js stays as it is" if args.quick else f"wrote {page}")
    print("; ".join(where_now))
    section, rows = view["workspace"], view["log"]
    carried = section.get("quick")
    if carried:
        print(f"workspace: carried from {carried['carried_from']}; the last tidy run and merge questions read now"
              if carried["carried_from"] else "workspace: not read yet (no view.json to carry it from)")
    if section.get("error"):
        print(f"workspace: not read ({section['error']})")
    elif "mac" in section:
        mac_view = section["mac"]
        mac_word = ("this machine" if mac_view.get("this_machine") else "not reached" if not mac_view["reached"]
                    else f"reached, but {mac_view['error']}" if mac_view.get("error") else "reached")
        fetched = f"fetch failed ({section['fetch_error']})" if section.get("fetch_error") else "fetched"
        print(f"workspace: GitHub {fetched}; Mac {mac_word}")
    prs = section.get("pull_requests")
    if prs:
        print(f"open pull requests: GitHub did not say ({prs['error']})" if prs["error"] else
              "open pull requests: " + (", ".join(f"{pr['number']} ({pr['node'] or 'no record names it'})"
                                                  for pr in prs["open"]) or "none"))
    print(f"log: {len(rows['rows'])} of {rows['total']} rows ({rows['counts']['attempt']} script runs, "
          f"{rows['counts']['work']} starts, {rows['counts']['upkeep']} tidy runs, {rows['counts']['record']} records "
          f"by hand)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
