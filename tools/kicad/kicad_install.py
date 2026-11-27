"""Where KiCad 10 is installed on this machine (Windows, macOS, or Linux).

Every repository script that needs a KiCad executable, library, or template
takes the path from here, so one machine's layout is never hardcoded into a
script that the other machine also runs.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import sys

KICAD_CONFIG_VERSION = "10.0"

if sys.platform == "darwin":
    _CONTENTS = pathlib.Path("/Applications/KiCad/KiCad.app/Contents")
    KICAD_CLI = _CONTENTS / "MacOS" / "kicad-cli"
    KICAD_PCBNEW = _CONTENTS / "Applications" / "pcbnew.app" / "Contents" / "MacOS" / "pcbnew"
    KICAD_PYTHON = _CONTENTS / "Frameworks" / "Python.framework" / "Versions" / "Current" / "bin" / "python3"
    KICAD_SHARE = _CONTENTS / "SharedSupport"
elif sys.platform.startswith("linux"):
    KICAD_CLI = pathlib.Path(os.environ.get("KICAD_CLI") or shutil.which("kicad-cli") or "/usr/bin/kicad-cli")
    KICAD_PCBNEW = pathlib.Path(os.environ.get("KICAD_PCBNEW") or shutil.which("pcbnew") or "/usr/bin/pcbnew")
    KICAD_PYTHON = pathlib.Path(
        os.environ.get("KICAD_PYTHON")
        or ("/usr/bin/python3" if pathlib.Path("/usr/bin/python3").is_file() else (shutil.which("python3") or "/usr/bin/python3"))
    )
    KICAD_SHARE = pathlib.Path("/usr/share/kicad")
else:
    _BIN = pathlib.Path(r"C:\Program Files\KiCad") / KICAD_CONFIG_VERSION / "bin"
    KICAD_CLI = _BIN / "kicad-cli.exe"
    KICAD_PCBNEW = _BIN / "pcbnew.exe"
    KICAD_PYTHON = _BIN / "python.exe"
    KICAD_SHARE = _BIN.parent / "share" / "kicad"

KICAD_TEMPLATES = KICAD_SHARE / "template"
KICAD_STOCK_FOOTPRINTS = KICAD_SHARE / "footprints"
