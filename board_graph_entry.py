#!/usr/bin/env python3
"""Console script entrypoint resolving editable checkouts and installed wheel data."""
from __future__ import annotations

import pathlib
import sys


def _resolve_scripts_dir(base: pathlib.Path | None = None) -> pathlib.Path:
    """Locate the scripts directory from an editable checkout or wheel _board_graph_data."""
    here = (base or pathlib.Path(__file__)).resolve()
    if here.is_file():
        here = here.parent
    for candidate in (here / "scripts", here / "_board_graph_data" / "scripts"):
        if (candidate / "board_graph_cli.py").is_file():
            return candidate
    raise RuntimeError(
        f"Could not find board_graph_cli.py under {here / 'scripts'} or {here / '_board_graph_data' / 'scripts'}"
    )


def main(argv: list[str] | None = None) -> int:
    scripts_dir = _resolve_scripts_dir()
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import board_graph_cli

    return board_graph_cli.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
