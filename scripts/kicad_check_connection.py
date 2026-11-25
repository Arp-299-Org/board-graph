"""Read two named pads and their copper connectivity from a saved KiCad board."""
from __future__ import annotations

import argparse
import json
import pathlib


def named_pad_groups(board, start: str, end: str, net: str) -> dict[str, list]:
    """Collect all pads matching `start` and `end` (allowing multi-pad same-number footprints)."""
    if start == end:
        raise ValueError(f"expected two distinct pads on the board: {start}, {end}")
    groups: dict[str, list] = {start: [], end: []}
    for pad in board.GetPads():
        name = f"{pad.GetParentFootprint().GetReference()}.{pad.GetNumber()}"
        if name in groups:
            if pad.GetNetname() != net:
                raise ValueError(f"{name} is on {pad.GetNetname()!r}, expected {net!r}")
            groups[name].append(pad)
    if not groups[start] or not groups[end]:
        raise ValueError(f"expected two distinct pads on the board: {start}, {end}")
    return groups


def named_pads(board, start, end, net):
    groups = named_pad_groups(board, start, end, net)
    for name in (start, end):
        if len(groups[name]) > 1:
            raise ValueError(f"{name} occurs more than once on this board")
    return {start: groups[start][0], end: groups[end][0]}


def connected(board, start, end, net):
    groups = named_pad_groups(board, start, end, net)
    board.BuildConnectivity()
    anchor = groups[start][0]
    reached = {anchor.m_Uuid.AsString()}
    reached.update(item.m_Uuid.AsString() for item in board.GetConnectivity().GetConnectedItems(anchor))
    return all(pad.m_Uuid.AsString() in reached for pad in (*groups[start], *groups[end]))


def main():
    import pcbnew
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("board")
    parser.add_argument("start")
    parser.add_argument("end")
    parser.add_argument("net")
    parser.add_argument("output", type=pathlib.Path)
    parser.add_argument("--centered-exits", action="store_true")
    args = parser.parse_args()
    board = pcbnew.LoadBoard(args.board)
    report = {"connected": connected(board, args.start, args.end, args.net)}
    if args.centered_exits:
        from pad_exits import measure
        pads = named_pads(board, args.start, args.end, args.net)
        report["pad_exits"] = measure(board, pads, args.net, args.output)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
