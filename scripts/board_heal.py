#!/usr/bin/env python3
"""Enforced 6-card self-healing gate and CLI (`board-graph heal ...`) wrapping dev/tool_dev.py."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import tool_paths  # noqa: E402
from no_window import KWARGS as NO_WINDOW  # noqa: E402

SKIP_GATE_ENV = "BOARD_GRAPH_SKIP_HEAL_GATE"
TOOL_GRAPH_REL = pathlib.PurePosixPath("dev/tool_graph.json")
HEAL_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def tool_dev_available(tool_root: pathlib.Path | None = None) -> bool:
    """True when `tool_root` carries the repo-local 6-card tool workflow under `dev/`."""
    root = pathlib.Path(tool_root or tool_paths.TOOL_ROOT)
    return (root / "dev" / "tool_dev.py").is_file() and (root / "dev" / "tool_graph.json").is_file()


def tool_code_hash(tool_root: pathlib.Path | None = None) -> str | None:
    """Compute the tool_code approval digest without executing board_graph module globals."""
    root = pathlib.Path(tool_root or tool_paths.TOOL_ROOT).resolve()
    actions_path = root / "dev" / "tool_actions.py"
    if not actions_path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("_bg_dev_tool_actions", str(actions_path))
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
        fp = mod.tool_code_fingerprint(root)
        return str(fp.get("digest")) if isinstance(fp, dict) and fp.get("digest") else None
    except Exception:  # noqa: BLE001
        return None


def _read_current(root: pathlib.Path) -> dict:
    cur_file = root / "logs" / "tool-dev" / "current.json"
    if not cur_file.is_file():
        return {}
    try:
        data = json.loads(cur_file.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _read_dev_db(state_path: pathlib.Path) -> tuple[list[dict], list[dict]]:
    if not state_path.is_file():
        return [], []
    uri = f"file:{state_path.as_posix()}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=2.0)) as conn:
            conn.row_factory = sqlite3.Row
            tr_rows = conn.execute(
                "SELECT seq, at, track, node, outcome, target, actor, note, evidence_json, inputs_json "
                "FROM transitions WHERE track='tool' ORDER BY seq"
            ).fetchall()
            att_rows = conn.execute(
                "SELECT run_id, node, actor, started_at, finished_at, note FROM attempts ORDER BY rowid"
            ).fetchall()
    except sqlite3.Error:
        return [], []
    transitions = []
    for row in tr_rows:
        item = dict(row)
        try:
            item["evidence"] = json.loads(item.pop("evidence_json") or "[]")
        except ValueError:
            item["evidence"] = []
        try:
            item["inputs"] = json.loads(item.pop("inputs_json") or "{}")
        except ValueError:
            item["inputs"] = {}
        transitions.append(item)
    attempts = [dict(row) for row in att_rows]
    return transitions, attempts


def _build_cards(nodes: list[dict], transitions: list[dict], attempts: list[dict], position: str | None) -> dict:
    last_by_node: dict[str, dict] = {}
    for tr in transitions:
        last_by_node[tr["node"]] = tr
    open_by_node: dict[str, dict] = {}
    for att in attempts:
        if not att.get("finished_at"):
            open_by_node[att["node"]] = att
    pos_idx = len(nodes) if position == "done" else next((i for i, n in enumerate(nodes) if n["id"] == position), 0)
    cards = []
    passed_count = 0
    working = None
    blocked_outcome = None
    blocked_node = None
    for idx, node in enumerate(nodes):
        nid = node["id"]
        last = last_by_node.get(nid)
        att = open_by_node.get(nid)
        if position == "done" or idx < pos_idx:
            status = "passed"
            passed_count += 1
        elif idx == pos_idx:
            status = "current"
            if att:
                working = att.get("actor")
            elif last and last.get("outcome") in ("needs_owner", "needs_lead", "needs_skill"):
                blocked_outcome = last.get("outcome")
                blocked_node = nid
        else:
            status = "ahead"
        cards.append({
            "id": nid,
            "label": node.get("label") or nid,
            "who": node.get("who"),
            "stage": node.get("stage"),
            "task": node.get("task"),
            "pass_when": node.get("pass_when"),
            "status": status,
            "working": att.get("actor") if (status == "current" and att) else None,
            "last_outcome": last.get("outcome") if last else None,
            "last_actor": last.get("actor") if last else None,
            "last_note": last.get("note") if last else None,
            "last_at": last.get("at") if last else None,
            "evidence": last.get("evidence", []) if last else [],
        })
    return {
        "cards": cards,
        "passed_count": passed_count,
        "working": working,
        "blocked_outcome": blocked_outcome,
        "blocked_node": blocked_node,
        "last_by_node": last_by_node,
    }


def tool_heal_summary(tool_root: pathlib.Path | None = None) -> dict | None:
    """Summarize the repo-local 6-card tool-fixing workflow and whether current tool edits passed it."""
    root = pathlib.Path(tool_root or tool_paths.TOOL_ROOT).resolve()
    tstatus = tool_paths.tool_status(root)
    has_dev = tool_dev_available(root)
    if not has_dev and not tstatus.get("dirty") and not tstatus.get("ahead"):
        return None
    graph_path = root / "dev" / "tool_graph.json"
    nodes: list[dict] = []
    if graph_path.is_file():
        try:
            gdata = json.loads(graph_path.read_text(encoding="utf-8"))
            nodes = [n for n in gdata.get("nodes", []) if n.get("track") == "tool"]
        except (OSError, ValueError):
            nodes = []
    cur = _read_current(root)
    version = cur.get("version")
    mode = cur.get("mode") or "release"
    transitions, attempts = [], []
    if version:
        state_path = root / "logs" / "tool-dev" / "releases" / str(version) / "state.sqlite3"
        transitions, attempts = _read_dev_db(state_path)
    start_id = nodes[0]["id"] if nodes else "triage"
    position = transitions[-1]["target"] if transitions else (start_id if version else None)
    done = position == "done"
    built = _build_cards(nodes, transitions, attempts, position) if nodes else {
        "cards": [], "passed_count": 0, "working": None, "blocked_outcome": None, "blocked_node": None, "last_by_node": {},
    }
    rel_tr = built["last_by_node"].get("release")
    verified_digest = None
    if rel_tr and rel_tr.get("outcome") == "pass":
        src_tc = ((rel_tr.get("inputs") or {}).get("approval_v1") or {}).get("source:tool_code") or {}
        if isinstance(src_tc, dict):
            verified_digest = src_tc.get("digest")
    cur_digest = tool_code_hash(root) if has_dev else None
    dirty = bool(tstatus.get("dirty"))
    ahead = int(tstatus.get("ahead") or 0)
    needs_heal = dirty or ahead > 0
    tool_code_current = bool(done and cur_digest and verified_digest == cur_digest and not dirty)
    gate_ok = (not needs_heal) or tool_code_current
    gate_reason = None
    if not gate_ok:
        if not has_dev:
            gate_reason = "dev/tool_graph.json missing while git has unverified changes"
        elif dirty and not version:
            gate_reason = "uncommitted tool changes with no active 6-card heal cycle"
        elif dirty:
            gate_reason = f"uncommitted tool changes in cycle '{version}' (stage 6 requires clean commit)"
        elif not version:
            gate_reason = f"{ahead} commit(s) ahead of origin/main with no 6-card heal cycle"
        elif not done:
            gate_reason = f"cycle '{version}' is still at '{position}', not 'done'"
        elif verified_digest != cur_digest:
            gate_reason = f"tool code changed after '{version}' was approved; rerun tool_checks -> release"
    return {
        "available": has_dev,
        "version": version,
        "mode": mode,
        "from_card": cur.get("from_card"),
        "board_project": cur.get("board_project"),
        "active": bool(version),
        "position": position,
        "done": done,
        "passed_count": built["passed_count"],
        "total_count": len(built["cards"]),
        "working": built["working"],
        "blocked_outcome": built["blocked_outcome"],
        "blocked_node": built["blocked_node"],
        "dirty": dirty,
        "ahead": ahead,
        "branch": tstatus.get("branch"),
        "commit": tstatus.get("commit"),
        "commits_ahead": tstatus.get("commits_ahead") or [],
        "code_digest": cur_digest,
        "verified_digest": verified_digest,
        "tool_code_current": tool_code_current,
        "gate_ok": gate_ok,
        "gate_reason": gate_reason,
        "cards": built["cards"],
    }


def _is_temp_or_fixture_graph(graph_path: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        resolved = graph_path.resolve()
    except OSError:
        resolved = graph_path
    tmp_root = pathlib.Path(tempfile.gettempdir()).resolve()
    fixtures_dir = (root / "scripts" / "fixtures").resolve()
    return resolved.is_relative_to(tmp_root) or resolved.is_relative_to(fixtures_dir)


def check_tool_heal_gate(
    tool_root: pathlib.Path | None = None,
    *,
    graph_path: pathlib.Path | None = None,
    command: str | None = None,
    outcome: str | None = None,
    bypass_test_skip: bool = False,
) -> str | None:
    """Return an error message if tool edits have not passed the 6-card self-healing workflow, else None."""
    if not bypass_test_skip and os.environ.get(SKIP_GATE_ENV) == "1":
        return None
    if command == "record" and outcome == "needs_lead":
        return None
    root = pathlib.Path(tool_root or tool_paths.TOOL_ROOT).resolve()
    if graph_path is not None:
        gp = pathlib.Path(graph_path).resolve()
        if gp == (root / TOOL_GRAPH_REL).resolve():
            return None
        if not bypass_test_skip and _is_temp_or_fixture_graph(gp, root):
            return None
    if not tool_paths.is_git_checkout(root):
        return None
    summary = tool_heal_summary(root)
    if not summary or summary["gate_ok"]:
        return None
    ver = summary.get("version")
    pos = summary.get("position")
    hint = (
        f"finish cycle '{ver}' (currently at '{pos}') through 'release'"
        if ver and not summary.get("done")
        else "walk the 6-card board with 'board-graph heal start <slug>'"
    )
    return (
        f"Self-healing gate blocked: {root} has {summary['gate_reason']}. "
        f"Walk the 6-card tool-fixing workflow ({hint}) with a clean git tree before advancing the PCB."
    )


def needs_lead_hint(where_view: dict, tool_root: pathlib.Path | None = None) -> str | None:
    """Return a clear self-healing instruction line when a track stands on `needs_lead`."""
    if not tool_dev_available(tool_root):
        return None
    for tr in where_view.get("tracks") or []:
        node = tr.get("node") or {}
        last = node.get("last") or {}
        if last.get("outcome") == "needs_lead":
            nid = node.get("id") or "card"
            return (
                f"Self-heal: {nid} stopped on needs_lead. If this is a board-graph tool bug, run "
                f"'board-graph heal start fix-{nid} --from-card {nid}' to walk the 6-card fixing board."
            )
    return None


def refresh_project_view(project_root: pathlib.Path | None = None, tool_root: pathlib.Path | None = None) -> None:
    """Best-effort quick view.json refresh for the active board project after mutating heal state."""
    root = pathlib.Path(tool_root or tool_paths.TOOL_ROOT).resolve()
    proj = pathlib.Path(project_root or tool_paths.project_root()).resolve()
    if proj == root:
        return
    if not any((proj / m).exists() for m in tool_paths.PROJECT_MARKERS):
        return
    view_script = root / "scripts" / "board_graph_view.py"
    if not view_script.is_file():
        return
    env = dict(os.environ)
    env[tool_paths.ENV] = str(proj)
    subprocess.run(
        [sys.executable, "-B", str(view_script), "snapshot", "--quick"],
        cwd=str(proj),
        env=env,
        capture_output=True,
        text=True,
        check=False,
        **NO_WINDOW,
    )


def _run_tool_dev(args: list[str], *, tool_root: pathlib.Path, project_root: pathlib.Path | None) -> int:
    env = dict(os.environ)
    env[tool_paths.ENV] = str(tool_root)
    done = subprocess.run(
        [sys.executable, "-B", str(tool_root / "dev" / "tool_dev.py"), *args],
        cwd=str(tool_root),
        env=env,
        check=False,
        **NO_WINDOW,
    )
    if args and args[0] in ("cycle", "next", "record", "settle"):
        refresh_project_view(project_root, tool_root)
    return done.returncode


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for `board-graph heal ...`."""
    raw = list(sys.argv[1:] if argv is None else argv)
    root = pathlib.Path(tool_paths.TOOL_ROOT).resolve()
    proj = tool_paths.project_root()
    if not raw or raw[0] in ("-h", "--help", "help"):
        print(
            "usage:\n"
            "  board-graph heal status [--json]\n"
            "  board-graph heal start <slug> [--from-card CARD] [--force]\n"
            "  board-graph heal <where|next|record|brief|approvals|settle|check|cycles> [args...]"
        )
        return 0
    if raw[0] == "status":
        p = argparse.ArgumentParser(prog="board-graph heal status")
        p.add_argument("--json", action="store_true")
        ns = p.parse_args(raw[1:])
        summary = tool_heal_summary(root)
        if ns.json:
            print(json.dumps(summary, indent=2))
        elif not summary:
            print("Self-healing status: clean release (no editable dev workflow)")
        else:
            print(
                f"Self-heal: cycle={summary['version'] or 'none'} mode={summary['mode']} "
                f"pos={summary['position'] or 'none'} ({summary['passed_count']}/{summary['total_count']}) "
                f"dirty={summary['dirty']} ahead={summary['ahead']} gate_ok={summary['gate_ok']}"
            )
            if summary["gate_reason"]:
                print(f"Blocked: {summary['gate_reason']}")
        return 0
    if not tool_dev_available(root):
        print(f"board-graph heal: {root / 'dev' / 'tool_dev.py'} is missing (wheel install)", file=sys.stderr)
        return 1
    if raw[0] == "start":
        p = argparse.ArgumentParser(prog="board-graph heal start")
        p.add_argument("slug", help="Short fix slug (e.g. fix-sync-pcb)")
        p.add_argument("--from-card", help="Board card that surfaced the bug")
        p.add_argument("--force", action="store_true")
        ns = p.parse_args(raw[1:])
        if not HEAL_SLUG_RE.fullmatch(ns.slug):
            print(f"invalid heal slug {ns.slug!r}", file=sys.stderr)
            return 2
        cmd = ["cycle", ns.slug, "--mode", "heal"]
        if ns.from_card:
            cmd.extend(["--from-card", ns.from_card])
        if proj != root:
            cmd.extend(["--board-project", str(proj)])
        if ns.force:
            cmd.append("--force")
        return _run_tool_dev(cmd, tool_root=root, project_root=proj)
    return _run_tool_dev(raw, tool_root=root, project_root=proj)


if __name__ == "__main__":
    raise SystemExit(main())
