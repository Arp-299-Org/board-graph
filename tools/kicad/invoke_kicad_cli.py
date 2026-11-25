#!/usr/bin/env python3
r"""Run KiCad CLI with every user-writable path isolated from Windows Documents.

kicad-cli initializes ``Documents\KiCad`` and a user config tree even for
read-only jobs, and with an empty config it cannot resolve KiCad's standard
libraries.  Every repository-owned CLI launch passes through this module so
KiCad receives writable document, configuration, and cache roots under %TEMP%
seeded with the stock library tables.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence


from kicad_install import KICAD_CLI, KICAD_CONFIG_VERSION as KICAD_VERSION, KICAD_TEMPLATES as KICAD_TEMPLATE_ROOT

RUNTIME_ROOT = pathlib.Path(tempfile.gettempdir()) / "board-graph-kicad-cli-runtime-v1"
STANDARD_LIBRARY_TABLES = ("sym-lib-table", "fp-lib-table")


def seed_standard_library_tables(config_home: pathlib.Path) -> None:
    """Install KiCad's read-only standard library tables in the isolated config.

    An empty ``KICAD_CONFIG_HOME`` prevents the Windows Documents crash, but it
    also makes otherwise-valid standard symbols and footprints look unresolved
    to CLI ERC.  Seed only KiCad's installed stock tables; project-local tables
    remain authoritative for project libraries.
    """

    versioned_config = config_home / KICAD_VERSION
    versioned_config.mkdir(parents=True, exist_ok=True)
    for table_name in STANDARD_LIBRARY_TABLES:
        source = KICAD_TEMPLATE_ROOT / table_name
        if not source.is_file():
            raise FileNotFoundError(f"KiCad standard library table not found at {source}")
        shutil.copyfile(source, versioned_config / table_name)


def isolated_environment() -> dict[str, str]:
    """Return a child environment whose KiCad write roots are all writable."""

    documents_home = RUNTIME_ROOT / "documents"
    config_home = RUNTIME_ROOT / "config"
    cache_home = RUNTIME_ROOT / "cache"
    for path in (documents_home, config_home, cache_home):
        path.mkdir(parents=True, exist_ok=True)
    seed_standard_library_tables(config_home)

    child_environment = os.environ.copy()
    child_environment["KICAD_DOCUMENTS_HOME"] = str(documents_home)
    child_environment["KICAD_CONFIG_HOME"] = str(config_home)
    child_environment["KICAD_CACHE_HOME"] = str(cache_home)
    return child_environment


def run_kicad_cli(
    arguments: Sequence[str],
    *,
    capture_output: bool = False,
    text: bool = True,
    cwd: pathlib.Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the installed KiCad 10 CLI without touching the user's Documents."""

    if not KICAD_CLI.is_file():
        raise FileNotFoundError(f"KiCad CLI not found at {KICAD_CLI}")
    # kicad-cli is a console program. Launched from an agent shell that has no
    # visible console, Windows allocates a new console window for it, which pops
    # over whatever the user is doing. Never let it create a window; its output
    # still flows through the inherited or captured pipes.
    spawn_options: dict = {}
    if sys.platform == "win32":
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        spawn_options["startupinfo"] = startup
        spawn_options["creationflags"] = subprocess.CREATE_NO_WINDOW
        # With no console, stdout/stderr only reach the caller when the standard
        # handles are passed explicitly; naming any one of them makes Python pass all three.
        spawn_options["stdin"] = subprocess.DEVNULL
    return subprocess.run(
        [str(KICAD_CLI), *map(str, arguments)],
        capture_output=capture_output,
        text=text,
        cwd=str(cwd) if cwd is not None else None,
        env=isolated_environment(),
        check=False,
        **spawn_options,
    )


def main() -> int:
    if len(sys.argv) == 1:
        print("usage: python tools/kicad/invoke_kicad_cli.py <KiCad CLI arguments>", file=sys.stderr)
        return 2
    completed = run_kicad_cli(sys.argv[1:])
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
