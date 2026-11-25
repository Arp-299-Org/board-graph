#!/bin/bash
# See the KiCad host screen from any session, over SSH (or X11 on Linux): the whole screen, or one window even
# while others cover it. It only looks; it never clicks, types or moves a window. macOS requires Screen Recording
# permission for /usr/libexec/sshd-keygen-wrapper. Run it on the host, for example:
#   py -3 scripts/mac.py run 'bash scripts/mac_screen.sh window "PCB Editor" /tmp/pcb.png 1800'
#
#   bash scripts/mac_screen.sh list                              windows on screen: number, app, title
#   bash scripts/mac_screen.sh screen <out.png> [width]          the whole screen
#   bash scripts/mac_screen.sh window <title text> <out.png> [width]
#                                                                the one window whose app or title has <title
#                                                                text>, e.g. "PCB Editor"; or #<number> from list
# [width] scales the picture down to that many pixels wide (sips); the screen itself is 3840 x 2160.
set -eu

# Every ordinary window on screen, one per line: number, app, title. Window titles need Screen Recording.
# Passed with -e, not a here-document, so listing writes nothing anywhere. macOS returns no list at all (not
# an empty one) when this session cannot reach the Mac's desktop; that is an error, never "no windows".
LIST_WINDOWS='ObjC.import("CoreGraphics");
const list = ObjC.castRefToObject($.CGWindowListCopyWindowInfo($.kCGWindowListOptionOnScreenOnly | $.kCGWindowListExcludeDesktopElements, $.kCGNullWindowID));
if (!list || list.isNil()) throw new Error("macOS returned no window list");
const rows = [];
for (let i = 0; i < list.count; i++) {
  const w = list.objectAtIndex(i);
  if (ObjC.unwrap(w.objectForKey("kCGWindowLayer")) !== 0) continue;
  rows.push([ObjC.unwrap(w.objectForKey("kCGWindowNumber")), ObjC.unwrap(w.objectForKey("kCGWindowOwnerName")) || "",
             ObjC.unwrap(w.objectForKey("kCGWindowName")) || ""].join("\t"));
}
rows.join("\n");'

OS=$(uname -s)

windows() {
  if [ "$OS" = "Linux" ]; then
    windows_json_linux | python3 -c '
import json, sys
for row in json.load(sys.stdin):
    print("\t".join([str(row["number"]), row["app"], row["title"]]))
'
    return 0
  fi
  local rows
  if ! rows=$(osascript -l JavaScript -e "$LIST_WINDOWS" 2>&1); then
    echo "Could not list the windows: ${rows:-no message}" >&2
    echo "This session may not reach the Mac's desktop (no one logged in to the screen, or a sandbox)." >&2
    exit 5
  fi
  printf '%s\n' "$rows"
}

windows_json_linux() {
  [ -n "${DISPLAY:-}" ] || { echo "DISPLAY is not set" >&2; exit 5; }
  command -v xdotool >/dev/null 2>&1 || { echo "xdotool is not installed" >&2; exit 5; }
  python3 -c '
import json, subprocess
out = subprocess.run(["xdotool", "search", "--onlyvisible", "--name", ""], capture_output=True, text=True, check=False)
rows = []
for raw in out.stdout.splitlines():
    wid = raw.strip()
    if not wid.isdigit():
        continue
    title = subprocess.run(["xdotool", "getwindowname", wid], capture_output=True, text=True, check=False).stdout.strip()
    if not title:
        continue
    geom = subprocess.run(["xdotool", "getwindowgeometry", "--shell", wid], capture_output=True, text=True, check=False).stdout
    vals = dict(line.split("=", 1) for line in geom.splitlines() if "=" in line)
    if not {"X", "Y", "WIDTH", "HEIGHT"}.issubset(vals):
        continue
    w, h = int(vals["WIDTH"]), int(vals["HEIGHT"])
    if w < 2 or h < 2:
        continue
    pid_out = subprocess.run(["xdotool", "getwindowpid", wid], capture_output=True, text=True, check=False)
    pid = int(pid_out.stdout.strip()) if pid_out.returncode == 0 and pid_out.stdout.strip().isdigit() else 0
    comm = subprocess.run(["ps", "-p", str(pid), "-o", "comm="], capture_output=True, text=True, check=False).stdout.strip() if pid else ""
    if comm == "pcbnew" or "PCB Editor" in title:
        app = "PCB Editor"
    elif comm == "eeschema" or "Schematic Editor" in title:
        app = "Schematic Editor"
    elif comm == "kicad":
        app = "KiCad"
    else:
        app = comm
    rows.append({"number": int(wid), "app": app, "pid": pid, "title": title,
                 "bounds": [int(vals["X"]), int(vals["Y"]), w, h]})
print(json.dumps(rows))
'
}

capture() {  # screencapture's arguments, the output file last; on failure, say what macOS said and what it may mean
  local error out="${!#}"
  if [ "$OS" = "Linux" ]; then
    [ -n "${DISPLAY:-}" ] || { echo "DISPLAY is not set" >&2; exit 5; }
    command -v import >/dev/null 2>&1 || { echo "ImageMagick import is not installed" >&2; exit 4; }
    if ! error=$(import "$@" 2>&1); then
      echo "import failed: ${error:-no message}" >&2
      exit 4
    fi
    if [ ! -s "$out" ]; then
      echo "import reported no error but wrote no picture to $out" >&2
      exit 4
    fi
    return 0
  fi
  if ! error=$(screencapture "$@" 2>&1); then
    echo "screencapture failed: ${error:-no message}" >&2
    case "$error" in
      *"could not create image from display"*)
        echo "macOS would not let this session read the screen because Screen Recording for" >&2
        echo "/usr/libexec/sshd-keygen-wrapper was off; if it persists, that permission is the first thing to check." >&2
        exit 3 ;;
      *"could not create image from window"*)
        echo "The window may have closed or changed since it was listed: run list again." >&2 ;;
    esac
    exit 4
  fi
  # screencapture can report success and write nothing, as when the folder does not exist.
  if [ ! -s "$out" ]; then
    echo "screencapture reported no error but wrote no picture to $out${error:+ ($error)}" >&2
    exit 4
  fi
}

shrink() {  # $1 file, $2 width or empty
  if [ "$OS" = "Linux" ]; then
    if [ -n "${2:-}" ] && command -v convert >/dev/null 2>&1; then
      convert "$1" -resize "${2}x${2}>" "$1"
    fi
    if command -v identify >/dev/null 2>&1; then
      echo "$1 $(identify -format '%w %h ' "$1")"
    else
      python3 -c 'import struct, sys; d = open(sys.argv[1], "rb").read(24); w, h = struct.unpack(">II", d[16:24]); print(f"{sys.argv[1]} {w} {h} ")' "$1"
    fi
    return 0
  fi
  if [ -n "${2:-}" ]; then sips -Z "$2" "$1" --out "$1" > /dev/null; fi
  echo "$1 $(sips -g pixelWidth -g pixelHeight "$1" | awk '/pixel/ {printf "%s ", $2}')"
}

case "${1:-}" in
  list)
    windows
    ;;
  list-json)
    if [ "$OS" = "Linux" ]; then
      windows_json_linux
    else
      osascript -l JavaScript -e "$LIST_WINDOWS"
    fi
    ;;
  screen)
    out="${2:?screen needs an output file}"
    rm -f "$out"  # a failed capture must never leave an older picture to be mistaken for this one
    if [ "$OS" = "Linux" ]; then
      capture -window root "$out"
    else
      capture -x "$out"
    fi
    shrink "$out" "${3:-}"
    ;;
  window)
    text="${2:?window needs title text or #number}"
    out="${3:?window needs an output file}"
    rm -f "$out"  # a failed capture must never leave an older picture to be mistaken for this one
    all=$(windows)
    case "$text" in
      \#[0-9]*) matches=$(printf '%s\n' "$all" | awk -F '\t' -v n="${text#\#}" '$1 == n') ;;
      *) matches=$(printf '%s\n' "$all" | awk -F '\t' -v t="$text" 'index($2, t) || index($3, t)') ;;
    esac
    count=$(printf '%s' "$matches" | grep -c . || true)
    if [ "$count" -ne 1 ]; then
      if [ "$count" -eq 0 ]; then
        echo "No window on screen matches \"$text\". On screen now:" >&2
      else
        echo "$count windows match \"$text\"; name one by its number, e.g. window \"#<number>\":" >&2
      fi
      printf '%s\n' "${matches:-$all}" >&2
      exit 2
    fi
    number=$(printf '%s' "$matches" | cut -f1)
    echo "window $number: $(printf '%s' "$matches" | cut -f2-)"
    if [ "$OS" = "Linux" ]; then
      capture -window "$number" "$out"
    else
      capture -x -o -l "$number" "$out"
    fi
    shrink "$out" "${4:-}"
    ;;
  *)
    sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
    exit 64
    ;;
esac
