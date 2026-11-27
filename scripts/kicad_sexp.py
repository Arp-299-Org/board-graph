#!/usr/bin/env python3
"""Minimal s-expression reader for KiCad files (schematic, symbol, board).

Parses into nested Python lists; atoms are strings (quoted strings are
unescaped, bare tokens kept as text). Scripts that read KiCad's schematic,
board, footprint and netlist files use it instead of regular expressions.
"""

from __future__ import annotations

import math


def parse(text: str, *, model_spans=None):
    """Parse one form. When requested, keep source spans of model paths on footprints."""
    pos = 0
    n = len(text)

    def skip_ws() -> None:
        nonlocal pos
        while pos < n and text[pos] in " \t\r\n":
            pos += 1

    def read(parent=None):
        nonlocal pos
        skip_ws()
        if pos >= n:
            raise ValueError("unexpected end of input")
        c = text[pos]
        if c == "(":
            pos += 1
            items = []
            while True:
                skip_ws()
                if pos >= n:
                    raise ValueError("unterminated list")
                if text[pos] == ")":
                    pos += 1
                    return items
                start = pos
                item = read(items[0] if items else None)
                if model_spans is not None and parent == "footprint" and items == ["model"]:
                    if not isinstance(item, str):
                        raise ValueError("Invalid footprint model path")
                    model_spans.append((start, pos, item))
                items.append(item)
        if c == '"':
            pos += 1
            out = []
            while pos < n:
                ch = text[pos]
                if ch == "\\" and pos + 1 < n:
                    nxt = text[pos + 1]
                    out.append({"n": "\n", "t": "\t", "\\": "\\", '"': '"'}.get(nxt, "\\" + nxt))
                    pos += 2
                    continue
                if ch == '"':
                    pos += 1
                    return "".join(out)
                out.append(ch)
                pos += 1
            raise ValueError("unterminated string")
        start = pos
        while pos < n and text[pos] not in " \t\r\n()":
            pos += 1
        return text[start:pos]

    return read()


def children(node, tag: str):
    """All direct children of `node` whose head atom is `tag`."""
    return [c for c in node if isinstance(c, list) and c and c[0] == tag]


def child(node, tag: str):
    found = children(node, tag)
    return found[0] if found else None


def copper_layers(board) -> list[str]:
    """A parsed board's copper layers by KiCad's own names, in stack order: F.Cu, the inner layers by number, B.Cu.
    The saved board lists them in its (layers ...) block, whatever a project has renamed them to."""
    names = [str(layer[1]) for layer in child(board, "layers") or []
             if isinstance(layer, list) and len(layer) > 1 and str(layer[1]).endswith(".Cu")]
    inner = sorted((name for name in names if name.startswith("In") and name[2:-3].isdigit()),
                   key=lambda name: int(name[2:-3]))
    return [name for name in ("F.Cu",) if name in names] + inner + [name for name in ("B.Cu",) if name in names]


def prop(node, name: str | tuple[str, ...]):
    """Value of a (property "name" "value" ...) child, or None (tries tuple names in order)."""
    names = (name,) if isinstance(name, str) else tuple(name)
    props = children(node, "property")
    for target in names:
        for c in props:
            if len(c) >= 3 and c[1] == target and c[2]:
                return c[2]
    for target in names:
        for c in props:
            if len(c) >= 3 and c[1] == target:
                return c[2]
    return None


def _canonical_angle(raw: str) -> str | None:
    try:
        value = float(raw)
    except ValueError:
        return None
    if not math.isfinite(value):
        return None
    normalized = round(value % 360.0, 6) % 360.0
    if normalized == 0.0:
        return "0"
    return f"{normalized:.6f}".rstrip("0").rstrip(".")


def normalize_angles(node):
    """Return a copy of a parsed S-expression with KiCad (at x y [angle] ...) angles reduced modulo 360."""
    if not isinstance(node, list):
        return node
    out = [normalize_angles(item) for item in node]
    if len(out) >= 4 and out[0] == "at" and isinstance(out[3], str):
        angle = _canonical_angle(out[3])
        if angle is not None:
            if angle == "0" and (len(out) == 4 or (len(out) == 5 and out[4] == "unlocked")):
                return out[:3] + out[4:]
            out[3] = angle
    return out
