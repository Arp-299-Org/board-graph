#!/bin/bash
set -eu

export DISPLAY="${DISPLAY:-:99}"
DISPLAY_NUM="${DISPLAY#:}"
LOCK_FILE="/tmp/.X${DISPLAY_NUM}-lock"
XVFB_PID=""
OPENBOX_PID=""

cleanup() {
  if [ -n "$OPENBOX_PID" ] && kill -0 "$OPENBOX_PID" 2>/dev/null; then
    kill "$OPENBOX_PID" 2>/dev/null || true
    wait "$OPENBOX_PID" 2>/dev/null || true
  fi
  if [ -n "$XVFB_PID" ] && kill -0 "$XVFB_PID" 2>/dev/null; then
    kill "$XVFB_PID" 2>/dev/null || true
    wait "$XVFB_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

mkdir -p /tmp/runtime-root
chmod 700 /tmp/runtime-root

if [ -e "$LOCK_FILE" ]; then
  rm -f "$LOCK_FILE"
fi

Xvfb "$DISPLAY" -screen 0 1920x1080x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
XVFB_PID=$!

ready=0
for _ in $(seq 1 50); do
  if xdotool getdisplaygeometry >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 0.1
done
if [ "$ready" -ne 1 ]; then
  echo "Xvfb failed to become ready on DISPLAY=$DISPLAY" >&2
  cat /tmp/xvfb.log >&2 || true
  exit 1
fi

openbox >/tmp/openbox.log 2>&1 &
OPENBOX_PID=$!

mkdir -p /root/.config/kicad/10.0 /tmp/kicad
python3 - <<'PY'
import json, pathlib, shutil
cfg_dir = pathlib.Path.home() / ".config" / "kicad" / "10.0"
cfg_dir.mkdir(parents=True, exist_ok=True)
template_dir = pathlib.Path("/usr/share/kicad/template")
fallbacks = {
    "sym-lib-table": "(sym_lib_table\n  (version 7)\n)\n",
    "fp-lib-table": "(fp_lib_table\n  (version 7)\n)\n",
    "design-block-lib-table": "(design_block_lib_table\n  (version 7)\n)\n",
}
for name, fallback in fallbacks.items():
    src = template_dir / name
    dst = cfg_dir / name
    if not dst.exists():
        if src.is_file():
            shutil.copy2(src, dst)
        else:
            dst.write_text(fallback, encoding="utf-8")

cfg_file = cfg_dir / "kicad_common.json"
data = json.loads(cfg_file.read_text(encoding="utf-8")) if cfg_file.is_file() else {}
data.setdefault("meta", {})["version"] = 6
data.setdefault("api", {})["enable_server"] = True
prompts = data.setdefault("do_not_show_again", {})
prompts["data_collection_prompt"] = True
prompts["update_check_prompt"] = True
data.setdefault("graphics", {})["canvas_type"] = 2
cfg_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

app_configs = {
    "kicad.json": {"meta": {"version": 0}, "system": {"check_for_kicad_updates": False, "first_run_shown": True}},
    "pcbnew.json": {"meta": {"version": 5}, "system": {"first_run_shown": True}},
    "eeschema.json": {"meta": {"version": 0}, "system": {"first_run_shown": True}},
    "fpedit.json": {"meta": {"version": 0}, "system": {"first_run_shown": True}},
    "symbol_editor.json": {"meta": {"version": 0}, "system": {"first_run_shown": True}},
    "gerbview.json": {"meta": {"version": 0}, "system": {"first_run_shown": True}},
    "pl_editor.json": {"meta": {"version": 0}, "system": {"first_run_shown": True}},
    "cvpcb.json": {"meta": {"version": 0}, "system": {"first_run_shown": True}},
    "3d_viewer.json": {"meta": {"version": 0}, "system": {"first_run_shown": True}},
    "kipython.json": {"meta": {"version": 0}, "system": {"first_run_shown": True}},
}
for fname, payload in app_configs.items():
    target = cfg_dir / fname
    if not target.exists():
        target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

hotkeys = cfg_dir / "user.hotkeys"
if not hotkeys.exists():
    hotkeys.write_text("", encoding="utf-8")
PY

"$@"
