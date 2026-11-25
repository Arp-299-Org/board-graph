#!/usr/bin/env python3
"""Refill zones on the explicitly named live KiCad project and save it."""

from __future__ import annotations

import argparse
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import kicad_ipc  # noqa: E402

project_identity = kicad_ipc.project_identity


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=pathlib.Path, required=True)
    args = parser.parse_args()

    board = kicad_ipc.connect_board(timeout_ms=180_000, expected_project=args.project)
    board.refill_zones()
    board.save()
    print(f"status=refilled board={board.document.board_filename}")


if __name__ == "__main__":
    main()
