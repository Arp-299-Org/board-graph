#!/usr/bin/env python3
"""Compare the current PCB with its Git baseline independent of item ordering.

The live IPC save path may reorder top-level board items.  This audit compares
each top-level UUID-bearing block by identity and compares all other root
children as a multiset, while allowing only named footprint references supplied
on the command line to differ.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import os
import pathlib
import re
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import kicad_rules_geom as rg  # noqa: E402
import kicad_sexp  # noqa: E402
import tool_paths  # noqa: E402


class CompareError(RuntimeError):
    pass


def direct_children(text: str) -> list[tuple[int, int, str]]:
    result: list[tuple[int, int, str]] = []
    depth = 0
    start: int | None = None
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "(":
            if depth == 1:
                start = index
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                raise CompareError("Board has unbalanced parentheses")
            if depth == 1 and start is not None:
                match = re.match(r"\([ \t\r\n]*([A-Za-z0-9_]+)", text[start : index + 1])
                result.append((start, index + 1, match.group(1) if match else ""))
                start = None
    if depth != 0 or in_string:
        raise CompareError("Board s-expression is incomplete")
    return result


def compact(text: str) -> str:
    """Discard formatting whitespace without joining distinct atoms."""
    result: list[str] = []
    in_string = False
    escaped = False
    pending_space = False
    for char in text:
        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char in " \t\r\n":
            pending_space = True
        else:
            if pending_space and result and result[-1] not in "()" and char not in "()":
                result.append(" ")
            result.append(char)
            pending_space = False
            if char == '"':
                in_string = True
    return "".join(result)


def footprint_reference(block: str) -> str | None:
    match = re.search(r'\([ \t\r\n]*property[ \t\r\n]+"Reference"[ \t\r\n]+"((?:\\.|[^"\\])*)"', block)
    return match.group(1) if match else None


def root_uuid(block: str) -> str | None:
    for start, end, head in direct_children(block):
        if head != "uuid":
            continue
        match = re.fullmatch(r'\(uuid[ \t\r\n]*"([0-9a-fA-F-]+)"\)', compact(block[start:end]))
        if match:
            return match.group(1).lower()
    return None


def _normalize_block(block: str, head: str) -> str:
    import json

    parsed = kicad_sexp.parse(block)
    node = (
        parsed[0]
        if (isinstance(parsed, list) and parsed and isinstance(parsed[0], list) and parsed[0] and str(parsed[0][0]) == head)
        else parsed
    )
    if isinstance(node, list):
        strip_heads: frozenset[str] = frozenset()
        if head == "footprint":
            strip_heads = rg.FOOTPRINT_HEADER
        elif head == "setup":
            strip_heads = frozenset({"zone_defaults"})
        if strip_heads:
            node = [node[0]] + [
                item
                for item in node[1:]
                if not (isinstance(item, list) and item and str(item[0]) in strip_heads)
            ]
        node = kicad_sexp.normalize_angles(node)
    return json.dumps(node, separators=(",", ":"))


def inventory(text: str, allowed_refs: set[str], allowed_heads: set[str]):
    keyed: dict[tuple[str, str], str] = {}
    unkeyed: Counter[tuple[str, str]] = Counter()
    allowed: dict[str, str] = {}
    allowed_root_ids: dict[str, set[str]] = {head: set() for head in allowed_heads}
    references: Counter[str] = Counter()
    for start, end, head in direct_children(text):
        if head in rg.TOP_LEVEL_HEADERS:
            continue
        block = text[start:end]
        normalized = _normalize_block(block, head)
        if head == "footprint":
            ref = footprint_reference(block)
            if ref:
                references[ref] += 1
            if ref in allowed_refs:
                allowed[ref] = normalized
                continue
        if head in allowed_heads:
            identity = root_uuid(block)
            if identity is None:
                raise CompareError(f"Allowed {head} block lacks a root UUID")
            allowed_root_ids[head].add(identity)
            continue
        identity = root_uuid(block)
        if identity:
            key = (head, identity)
            if key in keyed:
                raise CompareError(f"Duplicate root UUID {identity} for {head}")
            keyed[key] = normalized
        else:
            unkeyed[(head, normalized)] += 1
    return keyed, unkeyed, allowed, allowed_root_ids, references


def git_baseline(repo: pathlib.Path, board: pathlib.Path) -> str:
    relative = board.relative_to(repo).as_posix()
    result = subprocess.run(
        ["git", "show", f"HEAD:{relative}"],
        cwd=repo,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise CompareError(result.stderr.decode(errors="replace").strip())
    return result.stdout.decode("utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--board", type=pathlib.Path, required=True)
    parser.add_argument("--allow-footprint-ref", action="append", default=[])
    parser.add_argument("--allow-head", action="append", default=[])
    args = parser.parse_args()

    board = args.board.resolve()
    repo = (
        tool_paths.project_root()
        if os.environ.get(tool_paths.ENV)
        else pathlib.Path(__file__).resolve().parents[1]
    )
    if repo not in board.parents or not board.is_file():
        raise CompareError(f"Board must be an existing file under {repo}: {board}")
    allowed_refs = set(args.allow_footprint_ref)
    allowed_heads = set(args.allow_head)
    if not allowed_refs and not allowed_heads:
        raise CompareError("At least one allowed footprint reference or head is required")

    before = inventory(git_baseline(repo, board), allowed_refs, allowed_heads)
    after = inventory(board.read_text(encoding="utf-8"), allowed_refs, allowed_heads)
    before_keyed, before_unkeyed, before_allowed, before_allowed_roots, before_refs = before
    after_keyed, after_unkeyed, after_allowed, after_allowed_roots, after_refs = after

    if set(before_allowed) != allowed_refs or set(after_allowed) != allowed_refs:
        raise CompareError(
            f"Allowed footprint inventory mismatch: before={sorted(before_allowed)} "
            f"after={sorted(after_allowed)} expected={sorted(allowed_refs)}"
        )
    if before_refs != after_refs:
        difference = (before_refs - after_refs) + (after_refs - before_refs)
        raise CompareError(f"Footprint reference inventory changed: {dict(difference)}")
    if before_allowed_roots != after_allowed_roots:
        raise CompareError(
            f"Allowed root identity inventory changed: before={before_allowed_roots} "
            f"after={after_allowed_roots}"
        )
    missing = sorted(set(before_keyed) - set(after_keyed))
    added = sorted(set(after_keyed) - set(before_keyed))
    changed = sorted(
        key for key in set(before_keyed) & set(after_keyed)
        if before_keyed[key] != after_keyed[key]
    )
    if missing or added or changed:
        raise CompareError(
            f"Unapproved UUID-bearing board changes: missing={missing[:8]} "
            f"added={added[:8]} changed={changed[:8]}"
        )
    if before_unkeyed != after_unkeyed:
        removed = list((before_unkeyed - after_unkeyed).elements())[:8]
        inserted = list((after_unkeyed - before_unkeyed).elements())[:8]
        raise CompareError(
            f"Unapproved unkeyed board changes: removed={removed} inserted={inserted}"
        )

    print(
        f"status=semantic_compare_pass allowed_refs={','.join(sorted(allowed_refs))} "
        f"allowed_heads={','.join(sorted(allowed_heads))} "
        f"unchanged_uuid_blocks={len(before_keyed)} "
        f"unchanged_unkeyed_blocks={sum(before_unkeyed.values())}"
    )


if __name__ == "__main__":
    main()
