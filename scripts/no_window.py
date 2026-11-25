"""What keeps a program an agent starts from opening a window on the owner's screen.

A script run by an agent or a hook has no console of its own, so on Windows
every console program it starts would open one. Pass KWARGS to subprocess
(creationflags CREATE_NO_WINDOW and a STARTUPINFO that hides any window the
child would show, as AGENTS.md's Screen safeguard asks); off Windows both are
absent. FLAGS alone is the creationflags value.
"""

import subprocess
import sys

if sys.platform == "win32":
    FLAGS = subprocess.CREATE_NO_WINDOW
    STARTUPINFO = subprocess.STARTUPINFO()
    STARTUPINFO.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    STARTUPINFO.wShowWindow = subprocess.SW_HIDE
    KWARGS = {"creationflags": FLAGS, "startupinfo": STARTUPINFO}
else:
    FLAGS = 0
    STARTUPINFO = None
    KWARGS = {}
