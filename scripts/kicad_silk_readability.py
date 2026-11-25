#!/usr/bin/env python3
"""Apply a checked set of silk text changes to the selected live board.

The JSON plan names reference fields or free board texts and states both the
expected and desired values. Coordinates and text sizes are in millimetres;
stroke is in millimetres and angle in degrees. Only the named text objects
are updated. The saved source hash and live project identity must both match.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from kipy.geometry import Vector2  # noqa: E402
import kicad_ipc  # noqa: E402


def current(text):
    size = text.attributes.size
    pos = text.position
    return {
        "size": [round(size.x / 1_000_000, 6), round(size.y / 1_000_000, 6)],
        "stroke": round(text.attributes.stroke_width / 1_000_000, 6),
        "position": [round(pos.x / 1_000_000, 6), round(pos.y / 1_000_000, 6)],
        "angle": round(text.attributes.angle, 6),
    }


def apply_values(text, values):
    text.attributes.size = Vector2.from_xy(*(round(v * 1_000_000) for v in values["size"]))
    text.attributes.stroke_width = round(values["stroke"] * 1_000_000)
    text.position = Vector2.from_xy(*(round(v * 1_000_000) for v in values["position"]))
    text.attributes.angle = values["angle"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=pathlib.Path, required=True)
    parser.add_argument("--source-sha256", required=True)
    parser.add_argument("--plan", type=pathlib.Path, required=True)
    parser.add_argument("--inspect", action="store_true")
    args = parser.parse_args()
    expected_directory, expected_board = kicad_ipc.project_identity(args.project)
    board_path = expected_directory / expected_board
    digest = hashlib.sha256(board_path.read_bytes()).hexdigest()
    if digest.lower() != args.source_sha256.lower():
        raise RuntimeError(f"Saved board hash mismatch: {digest}")

    board = kicad_ipc.connect_board(timeout_ms=180_000, expected_project=args.project)

    plan = json.loads(args.plan.read_text(encoding="utf-8-sig"))
    footprints = {f.reference_field.text.value: f for f in board.get_footprints()}
    texts = list(board.get_text())
    updates = []
    for edit in plan["edits"]:
        if edit["kind"] == "reference":
            footprint = footprints[edit["name"]]
            field = footprint.reference_field
            if not field.visible:
                raise RuntimeError(f"Reference is hidden: {edit['name']}")
            item, text = footprint, field.text
        elif edit["kind"] == "board_text":
            found = [t for t in texts if getattr(t, "value", None) == edit["name"]]
            if len(found) != 1:
                raise RuntimeError(f"Expected one board text: {edit['name']}")
            item = text = found[0]
        else:
            raise RuntimeError(f"Unsupported edit kind: {edit['kind']}")
        before = current(text)
        if args.inspect:
            print(json.dumps({"name": edit["name"], "current": before}))
            continue
        if before == edit["to"]:
            continue
        if before != edit["from"]:
            raise RuntimeError(f"Unexpected source for {edit['name']}: {before}")
        apply_values(text, edit["to"])
        updates.append((edit, item))

    if args.inspect:
        return
    if not updates:
        print("status=already_applied")
        return
    commit = board.begin_commit()
    try:
        result = board.update_items([item for _, item in updates])
        if len(result) != len(updates):
            raise RuntimeError(f"Update returned {len(result)} item(s) for {len(updates)} edit(s)")
        for (edit, _), updated_item in zip(updates, result):
            updated = updated_item.reference_field.text if edit["kind"] == "reference" else updated_item
            if current(updated) != edit["to"]:
                raise RuntimeError(f"Update did not preserve target: {edit['name']}")
        board.push_commit(commit, "Silk readability")
    except Exception:
        board.drop_commit(commit)
        raise
    actual_footprints = {f.reference_field.text.value: f for f in board.get_footprints()}
    actual_texts = list(board.get_text())
    for edit, _ in updates:
        if edit["kind"] == "reference":
            actual = actual_footprints[edit["name"]].reference_field.text
        else:
            matches = [t for t in actual_texts if getattr(t, "value", None) == edit["name"]]
            if len(matches) != 1:
                raise RuntimeError(f"Updated board text missing: {edit['name']}")
            actual = matches[0]
        if current(actual) != edit["to"]:
            raise RuntimeError(f"Update did not reach live board: {edit['name']}")
    board.save()
    saved_sha256 = hashlib.sha256(board_path.read_bytes()).hexdigest()
    if saved_sha256 == digest:
        raise RuntimeError("Saved board hash did not change after text updates")
    print(json.dumps({"status": "saved", "updated": [e["name"] for e, _ in updates],
                      "board_sha256": saved_sha256}))


if __name__ == "__main__":
    main()
