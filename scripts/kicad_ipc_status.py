#!/usr/bin/env python3
"""Report the live KiCad IPC session state in one call.

Diagnoses the common blockers before live editor work:
API unreachable, API busy (usually a modal dialog such as Preferences),
wrong board open, or multiple KiCad instances competing for the socket.
Read-only.
"""

from __future__ import annotations

import subprocess
import sys


def process_count(name: str) -> int:
    if sys.platform != "win32":
        result = subprocess.run(["pgrep", "-x", name.removesuffix(".exe")],
                                capture_output=True, text=True, check=False)
        return len(result.stdout.split())
    # No window: this script itself runs without a console, so a console child would open one on the owner's screen.
    result = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    return sum(1 for line in result.stdout.splitlines() if name.lower() in line.lower())


def main() -> int:
    pcbnew_instances = process_count("pcbnew.exe")
    kicad_instances = process_count("kicad.exe")
    print(f"pcbnew_processes={pcbnew_instances} kicad_processes={kicad_instances}")
    if pcbnew_instances + kicad_instances == 0:
        print("status=no_kicad_running")
        return 1
    if pcbnew_instances > 1:
        print("warning=multiple_pcbnew_instances (socket may belong to either; keep one)")

    try:
        from kipy import KiCad
    except ImportError:
        print("status=live_environment_invalid")
        print("recovery=.\\scripts\\setup_kicad_ipc.ps1 -Confirm")
        return 1

    try:
        kicad = KiCad(timeout_ms=3_000)
        # The editor actually serving IPC, which the preflight compares with the installation it checked.
        running = kicad.get_version()
        board = kicad.get_board()
    except Exception as error:  # kipy raises ApiError subclasses of Exception
        message = str(error)
        if "busy" in message.lower():
            print("status=busy (a modal dialog is open in KiCad - close Preferences/DRC/etc.)")
        else:
            print(f"status=unreachable ({message.strip()})")
        return 1

    # The full version, not X.Y.Z rebuilt from its numbers: a test build reports 10.0.7-rc1, and must not pass as 10.0.7.
    print(f"kicad_version={running.full_version}")
    print(f"board_filename={board.document.board_filename}")
    print(f"project_path={board.document.project.path}")
    try:
        print(
            f"tracks={len(board.get_tracks())} vias={len(board.get_vias())} "
            f"nets={len(board.get_nets())}"
        )
    except Exception as error:
        if "busy" in str(error).lower():
            print("status=busy (identity readable, items blocked - a modal dialog is open; close it)")
        else:
            print(f"status=degraded ({str(error).strip()})")
        return 1
    print("status=ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
