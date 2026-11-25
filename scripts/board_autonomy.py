#!/usr/bin/env python3
"""Explicit opt-in autonomous delegation policy with a hard ceiling before order."""
from __future__ import annotations

MODES = ("supervised", "delegated")
NEVER_DELEGATED = frozenset({"order", "delivery", "assembly", "first_power", "subsystems", "product_judgment"})
DEFAULT_UNTIL = "quote_files"


def config(graph) -> dict:
    """Return normalized autonomy settings for `graph`, defaulting to supervised."""
    raw = graph.data.get("autonomy")
    return raw if isinstance(raw, dict) else {"mode": "supervised"}


def _track_order(graph, key: str) -> list[str]:
    node = graph.nodes.get(key)
    if not node:
        return []
    track = graph.tracks.get(node.get("track"), {})
    if not track.get("ordered"):
        return []
    return graph.track_nodes(node["track"])


def problems(graph) -> list[str]:
    """Validate optional top-level `autonomy` block in tools/board_graph.json."""
    if "autonomy" not in graph.data:
        return []
    raw = graph.data["autonomy"]
    if not isinstance(raw, dict):
        return ["autonomy must be an object"]
    mode = raw.get("mode")
    if mode not in MODES:
        return [f"autonomy.mode must be one of {', '.join(MODES)}"]
    if mode == "supervised":
        return []
    bad: list[str] = []
    if raw.get("authorized_by") != "owner":
        bad.append("autonomy.authorized_by must be 'owner' when mode is delegated")
    reason = raw.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        bad.append("autonomy.reason must be a non-empty string when mode is delegated")
    until = raw.get("until")
    if not isinstance(until, str) or until not in graph.nodes:
        bad.append(f"autonomy.until must name a known card: {until!r}")
        return bad
    if until in NEVER_DELEGATED:
        bad.append(f"autonomy.until cannot delegate {until}; order always requires owner")
        return bad
    order = _track_order(graph, until)
    if not order:
        bad.append(f"autonomy.until ({until}) must be on an ordered track")
        return bad
    if "order" in order and order.index(until) >= order.index("order"):
        bad.append("autonomy.until must come strictly before order")
    return bad


def is_delegated(graph, node: str) -> bool:
    """Return True when `node` sits at or before `autonomy.until` in delegated mode."""
    cfg = config(graph)
    if cfg.get("mode") != "delegated" or node in NEVER_DELEGATED:
        return False
    until = cfg.get("until")
    order = _track_order(graph, until) if isinstance(until, str) else []
    return node in order and order.index(node) <= order.index(until)


def can_delegate(graph, node: str, actor: str) -> bool:
    """Return True when an `agent:*` actor may pass an owner card under delegation."""
    return is_delegated(graph, node) and isinstance(actor, str) and actor.startswith("agent:")


def owner_gate_error(graph, node: str, actor: str) -> str | None:
    """Return refusal message if `actor` cannot pass owner card `node`, else None."""
    if can_delegate(graph, node, actor):
        return None
    if node in NEVER_DELEGATED and config(graph).get("mode") == "delegated":
        return f"{node} can never be delegated in autonomous mode; record it as the owner with the owner's words"
    return f"{node} is the owner's to pass; record it as the owner with the owner's words"


def check_target_gate(graph, state, root, views, node: str, outcome: str, actor: str) -> None:
    """Enforce owner authority, NEVER_DELEGATED ceiling, and upstream delegated blockers before recording."""
    spec = graph.nodes[node]
    if outcome == "pass" and spec["who"] == "owner" and actor not in ("owner", "script:unchanged"):
        err = owner_gate_error(graph, node, actor)
        if err:
            raise ValueError(err)
    if outcome == "pass" and node in NEVER_DELEGATED:
        review = state_review(graph, state, root, views)
        blockers = delegated_upstream_blockers(graph, review, node)
        if blockers:
            raise ValueError("; ".join(blockers))


def state_review(graph, state, root, views):
    import board_graph as bg

    return bg.approval_review(graph, state, root, views)


def delegated_inputs(graph, node: str, actor: str) -> dict:
    """Return audit fields stamped onto transition inputs when an agent passes an owner card."""
    spec = graph.nodes[node]
    if spec["who"] == "owner" and can_delegate(graph, node, actor):
        return {"delegated": True, "delegated_until": config(graph)["until"]}
    return {}


def summary(graph) -> dict | None:
    """Public autonomy summary for where/view JSON when delegated mode is active."""
    cfg = config(graph)
    if cfg.get("mode") != "delegated":
        return None
    return {
        "mode": "delegated",
        "until": cfg["until"],
        "authorized_by": cfg["authorized_by"],
        "reason": cfg["reason"],
    }


def banner(view: dict) -> str | None:
    """Render banner line for `where` text output when autonomous mode is active."""
    info = view.get("autonomy")
    if not info or info.get("mode") != "delegated":
        return None
    return f"Mode: AUTONOMOUS (delegated through {info['until']}; order always requires owner)"


def brief_note(graph, key: str) -> str | None:
    """Explain delegation status on `brief` for a delegated owner card."""
    if graph.nodes[key]["who"] == "owner" and is_delegated(graph, key):
        until = config(graph)["until"]
        return f"  Autonomy: delegated through {until} (agent may pass this card; order always requires owner)"
    return None


def render_header(view: dict, db_line: str | None) -> list[str]:
    """Build top header lines for `render(view)` including database and autonomous banner."""
    lines = [view["milestone"]]
    if db_line:
        lines.append(db_line)
    b = banner(view)
    if b:
        lines.append(b)
    lines.append("")
    return lines


def brief_header(graph, key: str, label: str, who: str, track_label: str) -> list[str]:
    """Build top header lines for `brief(...)` including delegation note when applicable."""
    lines = [f"Brief: {label} ({who}), card {key} on {track_label}"]
    note = brief_note(graph, key)
    if note:
        lines.append(note)
    return lines


def _has_delegated_pass(review, key: str) -> bool:
    substantive = [
        r for r in review.rows
        if r["node"] == key and not (r["outcome"] == "pass" and r["actor"] == "script:unchanged")
    ]
    passes = [r for r in substantive if r["outcome"] == "pass"]
    if not passes:
        return False
    return bool(passes[-1].get("inputs", {}).get("delegated"))


def delegated_upstream_blockers(graph, review, key: str) -> list[str]:
    """Block NEVER_DELEGATED cards whenever any upstream owner approval came from delegation."""
    if key not in NEVER_DELEGATED:
        return []
    import board_approvals

    seen: set[str] = set()
    todo = list(board_approvals.required(graph, key))
    while todo:
        cur = todo.pop()
        if cur in seen:
            continue
        seen.add(cur)
        todo.extend(board_approvals.required(graph, cur))
    order = _track_order(graph, key)
    if key in order:
        seen.update(order[:order.index(key)])

    flagged = sorted(name for name in seen if _has_delegated_pass(review, name))
    if not flagged:
        return []
    return [
        f"{key} cannot pass while upstream owner card(s) have delegated approvals: "
        f"{', '.join(flagged)}. Re-approve as owner before {key}"
    ]
