#!/usr/bin/env python3
"""Adoption guard and S-expression copper filtering for routing_run."""
from __future__ import annotations

import collections
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import kicad_rules_geom as rg  # noqa: E402
from kicad_sexp import child, normalize_angles, parse, prop  # noqa: E402

COPPER = ("segment", "arc", "via")
HEADER = rg.TOP_LEVEL_HEADERS
FILLS = ("filled_polygon", "fill_segments")


class Refused(Exception):
    pass


frozen = rg.frozen_sexp


def label(item) -> str:
    """A board item in a few words, for the guard's line."""
    kind = item[0]
    if kind == "footprint":
        return f"footprint {prop(item, 'Reference') or item[1]}"
    if kind == "zone":
        net, name = child(item, "net"), child(item, "name")
        what = "rule area" if child(item, "keepout") else "pour"
        return f"{what} {name[1] if name else ''}{' on ' + net[1] if net else ''}".replace("  ", " ").strip()
    if kind.startswith("gr_text") or kind == "text":
        return f"text {item[1]!r}" if len(item) > 1 and isinstance(item[1], str) else "a text"
    if kind.startswith("gr_"):
        return f"graphic {kind}"
    return kind


def is_copper_pour(item) -> bool:
    """True for top-level non-keepout KiCad copper zones on copper (.Cu / *.Cu / F&B.Cu) layers."""
    return rg.is_copper_pour_node(item)


def beyond_copper(text: str, *, allow_pours: bool = False) -> tuple:
    """Every top-level item that is not a track, arc or via (or copper pour when authorized), order ignored,
    with zone fills and the file's header left out: (a Counter of items, their labels)."""
    root = parse(text)
    if not isinstance(root, list) or not root or root[0] != "kicad_pcb":
        raise ValueError("not a KiCad board")
    net_map = rg.board_net_map(root)
    items, names = collections.Counter(), {}
    for item in root[1:]:
        if rg.skip_guard_top_level(item):
            continue
        if not isinstance(item, list) or not item:
            key = rg.guard_item_frozen(item, net_map)
            items[key] += 1
            names[key] = repr(item)
            continue
        head = item[0]
        if head in COPPER:
            continue
        if allow_pours and is_copper_pour(item):
            continue
        node = (
            [part for part in item if not (isinstance(part, list) and part and part[0] in FILLS)]
            if head == "zone"
            else item
        )
        key = rg.guard_item_frozen(node, net_map)
        items[key] += 1
        names[key] = label(item)
    return items, names


def guard(base: str, board: str, *, allow_pours: bool = False) -> list:
    """What changed besides allowed copper, one line per item; empty when the board may be adopted."""
    old, old_names = beyond_copper(base, allow_pours=allow_pours)
    new, new_names = beyond_copper(board, allow_pours=allow_pours)
    gone = collections.Counter(old_names[key] for key in (old - new).elements())
    came = collections.Counter(new_names[key] for key in (new - old).elements())
    lines = []
    for name in sorted(set(gone) | set(came)):
        both = min(gone[name], came[name])
        lines += [f"{name} changed"] * both
        lines += [f"{name} removed"] * (gone[name] - both)
        lines += [f"{name} added"] * (came[name] - both)
    return lines
