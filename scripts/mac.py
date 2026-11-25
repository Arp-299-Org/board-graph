#!/usr/bin/env python3
"""Drive the macOS KiCad host over SSH: its host, key and checkout come from the
site settings (scripts/site_settings.py).

The Mac's screen and KiCad are for agent-driven inspection and routing: any session may look,
click, type and open windows there, and never leaves unsaved edits in its KiCad. macOS requires Screen Recording
and Accessibility permissions for `/usr/libexec/sshd-keygen-wrapper`.

  py -3 scripts/mac.py run "git status --short"        a shell command in the Mac's checkout
  py -3 scripts/mac.py windows                         windows on screen: number, app, pid, bounds, title
  py -3 scripts/mac.py look ["Schematic Editor"|#n]    a picture of one window, or the whole screen, copied here
  py -3 scripts/mac.py menus pcbnew [View]             the menu items an editor offers
  py -3 scripts/mac.py menu pcbnew View "Zoom to Fit"  click a menu item (three names reach a submenu item)
  py -3 scripts/mac.py keys eeschema cmd+f escape      press keys: cmd, shift, opt, ctrl; escape, return, f1...
  py -3 scripts/mac.py type eeschema R38               type text
  py -3 scripts/mac.py mouse click 812 433 --pic       click at pixel (812, 433) of the last picture
  py -3 scripts/mac.py mouse scroll 960 540 3          3 wheel steps up at screen point (960, 540): KiCad zooms in
  py -3 scripts/mac.py open <file>.kicad_sch       open a file in its own editor window; prints its pid
  py -3 scripts/mac.py quit 4321                       Command-Q a window `open` started, if nothing is unsaved
  py -3 scripts/mac.py sync [--apply] [--by tidy]      bring the Mac's main level now and tell its page: a session
                                                        runs this right after its push to main; tidy runs it too

On the Mac itself the same commands run in a local shell instead
of over SSH.

Processes are named for System Events: pcbnew (PCB Editor), eeschema (Schematic Editor), or a pid. Menus
are the most reliable way to act; the Home key does not zoom to fit there, View > Zoom to Fit does.

Coordinates are screen points: 1920 x 1080, from the top left (the screen is 3840 x 2160 pixels). With --pic,
x and y are pixels of the last `look` picture instead. The tool converts them, brings that window forward,
and refuses when it has moved or closed since the picture, so a click never lands by stale geometry. What
the window shows may still have changed: look again after every action.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import pathlib
import shlex
import subprocess
import os
import sys
import tempfile
import time

import tool_paths

if __name__ == "__main__":
    tool_paths.take_argument()

import site_settings  # noqa: E402

# The machine setup: tools/board_graph_site.json, or the file BOARD_GRAPH_SITE names.
SITE = site_settings.load()
HOST = SITE.ssh_host
KEY = SITE.ssh_key
CHECKOUT = SITE.checkout
TOOL_CHECKOUT = SITE.tool_checkout
STATE = pathlib.Path(tempfile.gettempdir()) / "board-graph-mac"
LAST_LOOK = STATE / "last-look.json"


def tool_script(rel: str) -> str:
    """Path to a tool script on the KiCad machine: under TOOL_CHECKOUT or local TOOL_ROOT when external, else CHECKOUT."""
    if TOOL_CHECKOUT:
        return f"{TOOL_CHECKOUT}/{rel}"
    if LOCAL and tool_paths.is_external():
        return f"{tool_paths.TOOL_ROOT.as_posix()}/{rel}"
    return rel


def home_path(path: str) -> str:
    """A path on the Mac for a double-quoted shell word: a leading ~/ becomes $HOME/."""
    return f"$HOME/{path[2:]}" if path.startswith("~/") else path


CACHE = home_path(SITE.cache)  # the Mac's cache folder, as a shell word inside double quotes
OPENED = f"{CACHE}/opened"  # pids `open` started, the only ones `quit` will close
PATH = 'export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"\n'
EDITORS = ("KiCad", "PCB Editor", "Schematic Editor", "Symbol Editor", "Footprint Editor")
APPS = {".kicad_sch": "eeschema", ".kicad_pcb": "pcbnew"}
ONE_MACHINE = SITE.one_machine  # the project names no KiCad machine: this machine is the only one
LOCAL = ONE_MACHINE or sys.platform == "darwin"  # on the machine that holds the database: no SSH


class MacError(RuntimeError):
    pass


def ssh(program: str, stdin: str = "", timeout: int = 120) -> subprocess.CompletedProcess[bytes]:
    """Run `program` on the Mac: over SSH, or in a local shell on the Mac itself. Scripts travel on stdin, never
    through PowerShell, ssh and zsh quoting, and as bytes, so Windows does not turn their newlines into CRLF."""
    # No window: run by an agent, this script has no console, so a console child would open one on the owner's screen.
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    argv = ["/bin/sh", "-c", program] if LOCAL else \
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-i", str(KEY), HOST, program]
    try:
        result = subprocess.run(argv, input=stdin.encode(), capture_output=True, timeout=timeout, check=False,
                                creationflags=flags)
    except subprocess.TimeoutExpired as exc:
        raise MacError(f"the Mac did not finish within {timeout} s") from exc
    if result.returncode == 255 and not LOCAL:
        raise MacError(f"ssh failed: {text(result.stderr).strip()}")
    return result


def text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def bash(script: str, timeout: int = 120) -> str:
    result = ssh("bash -s", PATH + script, timeout)
    if result.returncode != 0:
        raise MacError((text(result.stderr) + text(result.stdout)).strip() or f"exit {result.returncode}")
    return text(result.stdout)


def osascript(script: str, javascript: bool = False) -> str:
    result = ssh("osascript -l JavaScript -" if javascript else "osascript -", script)
    said = (text(result.stderr) + text(result.stdout)).strip()
    if result.returncode != 0:
        if "assistive access" in said or "not allowed" in said:
            raise MacError(f"macOS refused ({said}). Accessibility for sshd-keygen-wrapper may be off: tell the owner.")
        raise MacError(said or f"osascript exit {result.returncode}")
    return text(result.stdout).strip()


# Every ordinary window: number, app, pid, title and bounds in points (titles need Screen Recording).
WINDOWS = '''ObjC.import("CoreGraphics");
const list = ObjC.castRefToObject($.CGWindowListCopyWindowInfo(%s, $.kCGNullWindowID));
if (!list || list.isNil()) throw new Error("macOS returned no window list");
const rows = [];
for (let i = 0; i < list.count; i++) {
  const w = list.objectAtIndex(i);
  const layer = ObjC.unwrap(w.objectForKey("kCGWindowLayer"));
  if (layer !== 0 && !(%s && layer === 8)) continue;
  const b = ObjC.deepUnwrap(w.objectForKey("kCGWindowBounds"));
  rows.push({number: ObjC.unwrap(w.objectForKey("kCGWindowNumber")), app: ObjC.unwrap(w.objectForKey("kCGWindowOwnerName")) || "",
             pid: ObjC.unwrap(w.objectForKey("kCGWindowOwnerPID")), title: ObjC.unwrap(w.objectForKey("kCGWindowName")) || "",
             bounds: [b.X, b.Y, b.Width, b.Height]});
}
JSON.stringify(rows);'''
SCREEN = 'ObjC.import("AppKit"); const f = $.NSScreen.mainScreen.frame; JSON.stringify([f.size.width, f.size.height]);'
MENUS = '''const p = %s;
const out = [];
for (const m of p.menuBars[0].menuBarItems()) {
  if (%s && m.name() !== %s) continue;
  out.push(m.name() + ": " + m.menus[0].menuItems.name().filter(n => n).join(" | "));
}
out.join("\\n");'''


_REMOTE_IS_LINUX: bool | None = None


def _remote_is_linux() -> bool:
    global _REMOTE_IS_LINUX
    if LOCAL:
        return sys.platform.startswith("linux")
    if _REMOTE_IS_LINUX is None:
        _REMOTE_IS_LINUX = bash("uname -s").strip() == "Linux"
    return _REMOTE_IS_LINUX


def pcb_window_title(title: str, board_stem: str) -> bool:
    """KiCad adds [Read Only] when the first edit updates its title under the runner's file lock."""
    clean = title.removeprefix("*")
    return clean in (
        f"{board_stem} — PCB Editor",
        f"{board_stem} [Read Only] — PCB Editor",
        f"{board_stem} - PCB Editor",
        f"{board_stem} [Read Only] - PCB Editor",
    )


def windows(every: bool = False, panels: bool = False) -> list[dict]:
    """On-screen windows. Include hidden windows with every, or floating panels such as router dimensions with panels."""
    if _remote_is_linux():
        return json.loads(bash(f"cd {CHECKOUT}\nbash {tool_script('scripts/mac_screen.sh')} list-json\n"))
    option = "$.kCGWindowListOptionAll" if every else "$.kCGWindowListOptionOnScreenOnly | $.kCGWindowListExcludeDesktopElements"
    return json.loads(osascript(WINDOWS % (option, "true" if panels else "false"), javascript=True))


def quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def process(name_or_pid: str) -> str:
    if name_or_pid.isdigit():
        return f"(first process whose unix id is {name_or_pid})"
    return f"process {quote(name_or_pid)}"


def act(name_or_pid: str, lines: list[str]) -> None:
    """Bring the process forward, then run AppleScript lines inside it."""
    body = "".join(f"  {line}\n" for line in lines)
    osascript(f'tell application "System Events" to tell {process(name_or_pid)}\n  set frontmost to true\n  delay 0.3\n{body}end tell\n')


KEY_CODES = {
    "escape": 53, "esc": 53, "return": 36, "enter": 36, "tab": 48, "space": 49, "delete": 51, "backspace": 51,
    "forwarddelete": 117, "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
    "left": 123, "right": 124, "down": 125, "up": 126,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100, "f9": 101, "f10": 109,
    "f11": 103, "f12": 111,
}
MODIFIERS = {"cmd": "command", "command": "command", "shift": "shift", "opt": "option", "option": "option",
             "alt": "option", "ctrl": "control", "control": "control"}


def key_line(combo: str) -> str:
    *mods, key = combo.split("+")
    try:
        using = ", ".join(f"{MODIFIERS[m.lower()]} down" for m in mods)
    except KeyError as exc:
        raise MacError(f"unknown modifier {exc} in {combo!r}") from None
    suffix = f" using {{{using}}}" if using else ""
    if key.lower() in KEY_CODES:
        return f"key code {KEY_CODES[key.lower()]}{suffix}"
    if len(key) == 1:
        return f"keystroke {quote(key)}{suffix}"
    raise MacError(f"unknown key {key!r} in {combo!r}")


def menu_line(names: list[str]) -> str:
    if len(names) == 2:
        top, item = names
        return f"click menu item {quote(item)} of menu {quote(top)} of menu bar item {quote(top)} of menu bar 1"
    if len(names) == 3:
        top, sub, item = names
        return (f"click menu item {quote(item)} of menu {quote(sub)} of menu item {quote(sub)} "
                f"of menu {quote(top)} of menu bar item {quote(top)} of menu bar 1")
    raise MacError("menu takes a menu and an item, or a menu, a submenu and an item")


def menus(name_or_pid: str, only: str | None) -> str:
    se = 'Application("System Events")'
    target = (f"{se}.processes.whose({{unixId: {name_or_pid}}})[0]" if name_or_pid.isdigit()
              else f"{se}.processes.byName({json.dumps(name_or_pid)})")
    return osascript(MENUS % (target, json.dumps(bool(only)), json.dumps(only or "")), javascript=True)


def find_window(target: str) -> dict:
    rows = windows()
    if target.startswith("#"):
        hits = [w for w in rows if str(w["number"]) == target[1:]]
    else:
        hits = [w for w in rows if target in w["app"] or target in w["title"]]
    if len(hits) != 1:
        listing = "\n".join(f'  #{w["number"]}  {w["app"]}  {w["title"]}' for w in rows)
        raise MacError(f'{len(hits)} windows on screen match "{target}"; name one as #number. On screen:\n{listing}')
    return hits[0]


def look(target: str | None, width: int, out: pathlib.Path | None) -> None:
    remote = "/tmp/board-graph-mac-look.png"
    if target:
        window = find_window(target)
        what = f'window #{window["number"]} ({window["app"]}: {window["title"]})'
        bounds, number, pid = window["bounds"], window["number"], window["pid"]
        command = f'window "#{number}" {remote} {width}'
    else:
        if _remote_is_linux():
            dims = [int(v) for v in bash("xdotool getdisplaygeometry").split()]
            screen_bounds = [0, 0, dims[0], dims[1]]
        else:
            screen_bounds = [0, 0, *json.loads(osascript(SCREEN, javascript=True))]
        what, bounds, number, pid = "the whole screen", screen_bounds, None, None
        command = f"screen {remote} {width}"
    report = bash(f"cd {CHECKOUT}\nbash {tool_script('scripts/mac_screen.sh')} {command}\n").strip().splitlines()[-1].split()
    pixels = [int(report[1]), int(report[2])]
    STATE.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now()
    out = out or STATE / f"look-{stamp:%Y%m%d-%H%M%S}.png"
    picture = ssh(f"cat {remote}")
    if picture.returncode != 0 or not picture.stdout:
        raise MacError(f"could not copy the picture back: {text(picture.stderr).strip() or 'empty'}")
    out.write_bytes(picture.stdout)
    LAST_LOOK.write_text(json.dumps({"file": str(out), "window": number, "pid": pid, "bounds": bounds,
                                     "pixels": pixels, "time": stamp.timestamp()}))
    print(out)
    print(f"{pixels[0]} x {pixels[1]} px of {what}; 1 px = {bounds[2] / pixels[0]:.3f} points. "
          "Aim at this picture's pixels with --pic.")


def to_points(x: float, y: float) -> tuple[float, float, int | None]:
    """Pixels of the last picture to screen points, refusing a window that has moved or closed since."""
    try:
        last = json.loads(LAST_LOOK.read_text())
    except FileNotFoundError:
        raise MacError("no picture to aim at: run look first") from None
    x0, y0, w, h = last["bounds"]
    if last["window"] is not None:
        now = next((r for r in windows(every=True) if r["number"] == last["window"]), None)
        if now is None:
            raise MacError(f'window #{last["window"]} has closed since the picture: look again')
        if now["bounds"] != last["bounds"]:
            raise MacError(f'window #{last["window"]} moved or resized since the picture: look again')
    age = time.time() - last["time"]
    if age > 120:
        print(f"note: the picture is {age:.0f} s old", file=sys.stderr)
    return x0 + x * w / last["pixels"][0], y0 + y * h / last["pixels"][1], last["pid"]


# Mouse events at screen points through CoreGraphics. Compiled once per version into the Mac's cache folder.
SWIFT = r'''import Foundation
import CoreGraphics

// move X Y | click X Y COUNT left|right | scroll X Y STEPS | drag X1 Y1 X2 Y2
let a = CommandLine.arguments
let source = CGEventSource(stateID: .hidSystemState)
func point(_ i: Int) -> CGPoint { CGPoint(x: Double(a[i])!, y: Double(a[i + 1])!) }
func post(_ type: CGEventType, _ at: CGPoint, _ button: CGMouseButton = .left, clicks: Int64 = 0) {
    let event = CGEvent(mouseEventSource: source, mouseType: type, mouseCursorPosition: at, mouseButton: button)!
    if clicks > 0 { event.setIntegerValueField(.mouseEventClickState, value: clicks) }
    event.post(tap: .cghidEventTap)
    usleep(30_000)
}
switch a[1] {
case "move":
    post(.mouseMoved, point(2))
case "click":
    let at = point(2), count = Int64(a[4])!, right = a[5] == "right"
    post(.mouseMoved, at)
    for n in 1...count {
        post(right ? .rightMouseDown : .leftMouseDown, at, right ? .right : .left, clicks: n)
        post(right ? .rightMouseUp : .leftMouseUp, at, right ? .right : .left, clicks: n)
    }
case "scroll":
    let at = point(2), steps = Int32(a[4])!
    post(.mouseMoved, at)
    usleep(100_000)
    for _ in 0..<abs(steps) {
        CGEvent(scrollWheelEvent2Source: source, units: .line, wheelCount: 1, wheel1: steps > 0 ? 1 : -1,
                wheel2: 0, wheel3: 0)!.post(tap: .cghidEventTap)
        usleep(80_000)
    }
case "drag":
    let from = point(2), to = point(4)
    post(.mouseMoved, from)
    post(.leftMouseDown, from)
    for i in 1...20 {
        let t = Double(i) / 20
        post(.leftMouseDragged, CGPoint(x: from.x + (to.x - from.x) * t, y: from.y + (to.y - from.y) * t))
    }
    post(.leftMouseUp, to)
default:
    FileHandle.standardError.write("unknown mouse action\n".data(using: .utf8)!)
    exit(64)
}
'''


def mouse(action: str, numbers: list[float], pic: bool, count: int, right: bool) -> None:
    needed = {"move": 2, "click": 2, "scroll": 3, "drag": 4}[action]
    if len(numbers) != needed:
        raise MacError(f"mouse {action} takes {needed} numbers")
    points, pid = list(numbers), None
    if pic:
        for i in range(0, 4 if action == "drag" else 2, 2):
            points[i], points[i + 1], pid = to_points(points[i], points[i + 1])
    if pid is not None:
        osascript(f'tell application "System Events" to set frontmost of {process(str(pid))} to true\ndelay 0.3\n')
    args = [action, *(f"{p:.1f}" for p in points[:needed - (action == "scroll")])]
    if action == "scroll":
        args.append(str(int(numbers[2])))
    if action == "click":
        args += [str(count), "right" if right else "left"]
    digest = hashlib.sha256(SWIFT.encode()).hexdigest()[:12]
    bash(f'''set -e
d="{CACHE}"; b="$d/mouse-{digest}"
if [ ! -x "$b" ]; then
  mkdir -p "$d"
  cat > "$b.swift" <<'SWIFT'
{SWIFT}SWIFT
  swiftc -O -o "$b" "$b.swift"
fi
"$b" {" ".join(shlex.quote(a) for a in args)}
''', timeout=300)
    print(f"mouse {' '.join(args)} (screen points)")


def open_file(path: str) -> tuple[str, dict | None]:
    """Open a schematic or board in a new editor process: its pid, and its window once the title names the file
    (None if none does after 30 s)."""
    if path[1:3] in (":/", ":\\"):
        raise MacError(f"{path} is a Windows path: Git Bash rewrites /tmp/... arguments unless MSYS_NO_PATHCONV=1")
    app = APPS.get(pathlib.PurePosixPath(path).suffix)
    if app is None:
        raise MacError(f"open takes a .kicad_sch or .kicad_pcb file, not {path}")
    bundle = f"/Applications/KiCad/KiCad.app/Contents/Applications/{app}.app"
    pid = bash(f'''cd {CHECKOUT}
f={shlex.quote(path)}
[ -f "$f" ] || {{ echo "no such file on the Mac: $f" >&2; exit 2; }}
before=$(pgrep -f "{bundle}/Contents/MacOS/" | sort)
open -n -a {bundle} "$f"
for i in $(seq 1 40); do
  new=$(comm -13 <(echo "$before") <(pgrep -f "{bundle}/Contents/MacOS/" | sort) | head -1)
  [ -n "$new" ] && break
  sleep 0.5
done
[ -n "$new" ] || {{ echo "no new {app} process started" >&2; exit 3; }}
mkdir -p "$(dirname "{OPENED}")"; echo "$new" >> "{OPENED}"
echo "$new"
''').strip()
    stem = pathlib.PurePosixPath(path).stem  # the editor shows "untitled" until the file has loaded
    for _ in range(60):
        titled = [w for w in windows() if str(w["pid"]) == pid and w["title"].startswith(f"{stem} ")]
        if titled:
            return pid, titled[0]
        time.sleep(0.5)
    return pid, None


def quit_app(pid: str) -> None:
    if pid not in bash(f'cat "{OPENED}" 2>/dev/null || true').split():
        raise MacError(f"{pid} was not started by `mac.py open`; this tool only closes windows it opened")
    unsaved = [w["title"] for w in windows(every=True) if str(w["pid"]) == pid and w["title"].startswith("*")]
    if unsaved:
        raise MacError(f"{pid} has unsaved edits ({unsaved[0]}); not quitting")
    act(pid, ['keystroke "q" using {command down}'])
    bash(f'''for i in $(seq 1 30); do kill -0 {pid} 2>/dev/null || break; sleep 0.5; done
kill -0 {pid} 2>/dev/null && {{ echo "{pid} is still running: look at the screen" >&2; exit 4; }}
grep -vx {pid} "{OPENED}" > "{OPENED}.new" || true; mv "{OPENED}.new" "{OPENED}"
''')


def sync(apply: bool, by: str = "push", checkout: str = CHECKOUT) -> None:
    """Bring the Mac's main level with GitHub's main now: one run of scripts/mac_keep_pace.py over SSH, which a
    session makes right after its push to main (`by` push) and tidy makes on its run (`by` tidy). Over SSH the
    run can read KiCad's window titles, so it also does the fast-forward and the reload the sync workflow's run
    on the Mac's runner leaves when KiCad has a changed file open. Prints its line."""
    if TOOL_CHECKOUT:
        script = f'cd {checkout}\nBOARD_GRAPH_PROJECT="$PWD" python3 {TOOL_CHECKOUT}/scripts/mac_keep_pace.py --by {by}'
    else:
        script = f"cd {checkout}\npython3 scripts/mac_keep_pace.py --by {by}"
    result = ssh("bash -s", PATH + script + ("" if apply else " --dry-run") + "\n", timeout=900)
    said = text(result.stdout).strip().splitlines()
    if said:
        print(f"Mac: keep-pace {said[-1]}")
    else:
        print(f"Mac: keep-pace did not run (exit {result.returncode}): {text(result.stderr).strip()}")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # KiCad's titles hold an em dash
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Drive the KiCad Mac host over SSH.")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run", help="a shell command in the Mac's checkout")
    p.add_argument("shell")
    p.add_argument("--timeout", type=int, default=600)
    sub.add_parser("windows", help="windows on screen")
    p = sub.add_parser("look", help="a picture of a window or the whole screen")
    p.add_argument("target", nargs="?", help='app or title text, or #number from windows; none for the screen')
    p.add_argument("--width", type=int, default=1600)
    p.add_argument("--out", type=pathlib.Path)
    p = sub.add_parser("menus", help="menu items of a process")
    p.add_argument("process")
    p.add_argument("menu", nargs="?")
    p = sub.add_parser("menu", help="click a menu item")
    p.add_argument("process")
    p.add_argument("names", nargs="+")
    p = sub.add_parser("keys", help="press keys")
    p.add_argument("process")
    p.add_argument("combos", nargs="+")
    p = sub.add_parser("type", help="type text")
    p.add_argument("process")
    p.add_argument("text")
    p = sub.add_parser("mouse", help="move, click, scroll or drag")
    p.add_argument("action", choices=["move", "click", "scroll", "drag"])
    p.add_argument("numbers", nargs="+", type=float)
    p.add_argument("--pic", action="store_true", help="x and y are pixels of the last look picture")
    p.add_argument("--count", type=int, default=1, help="clicks: 2 for a double click")
    p.add_argument("--right", action="store_true")
    p = sub.add_parser("open", help="open a schematic or board in its own editor window")
    p.add_argument("path", help="relative to the Mac's checkout, or absolute there")
    p = sub.add_parser("quit", help="Command-Q a window that open started")
    p.add_argument("pid")
    p = sub.add_parser("sync", help="fast-forward the Mac's main now (after a push to main) and tell its page")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--by", default="push", help="who makes this run: push (default) or tidy")
    args = parser.parse_args()

    try:
        if args.command == "run":
            result = ssh("bash -s", PATH + f"cd {CHECKOUT}\n{args.shell}\n", args.timeout)
            sys.stdout.write(text(result.stdout))
            sys.stderr.write(text(result.stderr))
            return result.returncode
        if args.command == "windows":
            for w in windows():
                x, y, width, height = (round(v) for v in w["bounds"])
                print(f'#{w["number"]}\t{w["app"]}\tpid {w["pid"]}\t{x},{y} {width}x{height}\t{w["title"]}')
        elif args.command == "look":
            look(args.target, args.width, args.out)
        elif args.command == "menus":
            print(menus(args.process, args.menu))
        elif args.command == "menu":
            act(args.process, [menu_line(args.names)])
        elif args.command == "keys":
            act(args.process, [key_line(c) for c in args.combos])
        elif args.command == "type":
            act(args.process, [f"keystroke {quote(args.text)}"])
        elif args.command == "mouse":
            mouse(args.action, args.numbers, args.pic, args.count, args.right)
        elif args.command == "open":
            pid, window = open_file(args.path)
            print(f'{pid}: window #{window["number"]} {window["title"]}' if window else
                  f"{pid}: started, but no window titled {pathlib.PurePosixPath(args.path).stem} yet: look before using it")
        elif args.command == "quit":
            quit_app(args.pid)
            print(f"{args.pid} quit")
        elif args.command == "sync":
            sync(args.apply, args.by)
    except MacError as exc:
        print(f"mac.py: {exc}", file=sys.stderr)
        return 1
    return 0


def report_routing_handoff():
    """A completed CLI gesture can carry the operator's handoff request on stderr."""
    deadline = os.environ.get("ROUTING_HANDOFF_AT")
    if deadline and time.time() >= float(deadline):
        print("Routing handoff requested. Finish or cancel the current gesture, leave KiCad idle "
              "and return now for inspection.", file=sys.stderr)


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        report_routing_handoff()
