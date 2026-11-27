#!/usr/bin/env bash
set -euo pipefail

PROVIDER=""
REPO_URL="https://github.com/Arp-299-Org/board-graph.git"
BRANCH=""
INSTALL_DIR="/opt/board-graph"
DISPLAY_NUM="${DISPLAY:-:99}"

usage() {
  cat <<'EOF'
Usage: bootstrap_linux_vm.sh --provider <claude|codex|gemini> [--repo URL] [--branch NAME] [--dir PATH]
EOF
  exit 2
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --provider) PROVIDER="${2:-}"; shift 2 ;;
    --repo) REPO_URL="${2:-}"; shift 2 ;;
    --branch) BRANCH="${2:-}"; shift 2 ;;
    --dir) INSTALL_DIR="${2:-}"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "Unknown argument: $1" >&2; usage ;;
  esac
done

case "$PROVIDER" in
  claude|codex|gemini) ;;
  *) echo "Error: --provider must be claude, codex, or gemini" >&2; usage ;;
esac

if [ -z "$BRANCH" ]; then
  BRANCH="exp/rj45-${PROVIDER}"
fi

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  SUDO="sudo"
fi

echo "==> Installing Ubuntu 24.04 + KiCad 10 + headless Xvfb dependencies..."
$SUDO apt-get update
$SUDO DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  ca-certificates curl dbus-x11 git gnupg imagemagick libgl1-mesa-dri \
  openbox python3 python3-pip python3-venv rsync software-properties-common \
  xdotool xvfb

if ! apt-cache policy kicad | grep -q "kicad-10.0-releases"; then
  $SUDO add-apt-repository -y ppa:kicad/kicad-10.0-releases
fi

if ! command -v pwsh >/dev/null 2>&1; then
  TMP_DEB="$(mktemp /tmp/ms-prod-XXXXXX.deb)"
  curl -sSL -o "$TMP_DEB" https://packages.microsoft.com/config/ubuntu/24.04/packages-microsoft-prod.deb
  $SUDO dpkg -i "$TMP_DEB"
  rm -f "$TMP_DEB"
fi

if ! command -v gh >/dev/null 2>&1; then
  $SUDO mkdir -p -m 755 /etc/apt/keyrings
  curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | $SUDO tee /etc/apt/keyrings/githubcli-archive-keyring.gpg >/dev/null
  $SUDO chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    | $SUDO tee /etc/apt/sources.list.d/github-cli.list >/dev/null
fi

$SUDO apt-get update
$SUDO DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  kicad kicad-symbols kicad-footprints powershell gh

if ! command -v node >/dev/null 2>&1 || [ "$(node -v | sed -E 's/^v([0-9]+).*/\1/')" -lt 20 ]; then
  echo "==> Installing Node.js 22 LTS..."
  curl -fsSL https://deb.nodesource.com/setup_22.x | $SUDO -E bash -
  $SUDO apt-get install -y --no-install-recommends nodejs
fi

echo "==> Installing provider CLIs (@anthropic-ai/claude-code, @openai/codex)..."
$SUDO npm install -g @anthropic-ai/claude-code @openai/codex

echo "==> Preparing board-graph checkout at ${INSTALL_DIR} on branch ${BRANCH}..."
if [ ! -d "${INSTALL_DIR}/.git" ]; then
  $SUDO mkdir -p "$(dirname "$INSTALL_DIR")"
  $SUDO git clone "$REPO_URL" "$INSTALL_DIR"
  $SUDO chown -R "$(id -u):$(id -g)" "$INSTALL_DIR"
else
  git -C "$INSTALL_DIR" fetch --all --prune
fi

if git -C "$INSTALL_DIR" show-ref --verify --quiet "refs/heads/${BRANCH}"; then
  git -C "$INSTALL_DIR" checkout "$BRANCH"
elif git -C "$INSTALL_DIR" show-ref --verify --quiet "refs/remotes/origin/${BRANCH}"; then
  git -C "$INSTALL_DIR" checkout -b "$BRANCH" "origin/${BRANCH}"
else
  git -C "$INSTALL_DIR" checkout -b "$BRANCH"
fi

echo "==> Building KiCad IPC venv and editable board-graph install..."
if [ ! -x "${INSTALL_DIR}/.venv-kicad-ipc/bin/python" ]; then
  /usr/bin/python3 -m venv "${INSTALL_DIR}/.venv-kicad-ipc"
fi
"${INSTALL_DIR}/.venv-kicad-ipc/bin/pip" install --upgrade pip setuptools wheel
"${INSTALL_DIR}/.venv-kicad-ipc/bin/pip" install -r "${INSTALL_DIR}/scripts/kicad-ipc-requirements.txt"
"${INSTALL_DIR}/.venv-kicad-ipc/bin/pip" install --no-cache-dir --no-deps --no-build-isolation -e "$INSTALL_DIR"

echo "==> Configuring KiCad 10 headless IPC settings..."
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

for fname, ver in (
    ("kicad.json", 0), ("pcbnew.json", 5), ("eeschema.json", 0), ("fpedit.json", 0),
    ("symbol_editor.json", 0), ("gerbview.json", 0), ("pl_editor.json", 0),
    ("cvpcb.json", 0), ("3d_viewer.json", 0), ("kipython.json", 0),
):
    target = cfg_dir / fname
    if not target.exists():
        payload = {"meta": {"version": ver}, "system": {"first_run_shown": True}}
        if fname == "kicad.json":
            payload["system"]["check_for_kicad_updates"] = False
        target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
PY

mkdir -p /tmp/kicad "${HOME}/steps"
export DISPLAY="${DISPLAY_NUM}"
export LIBGL_ALWAYS_SOFTWARE=1
export NO_AT_BRIDGE=1
export PATH="${INSTALL_DIR}/.venv-kicad-ipc/bin:${PATH}"

if ! pgrep -f "Xvfb ${DISPLAY_NUM}" >/dev/null 2>&1; then
  nohup Xvfb "${DISPLAY_NUM}" -screen 0 1920x1080x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
  sleep 1
fi
if ! pgrep -x openbox >/dev/null 2>&1; then
  nohup openbox >/tmp/openbox.log 2>&1 &
  sleep 1
fi

echo ""
echo "==> Bootstrap complete for provider: ${PROVIDER} (branch: ${BRANCH})"
echo "1. Verify headless KiCad IPC smoke test:"
echo "   DISPLAY=${DISPLAY_NUM} LIBGL_ALWAYS_SOFTWARE=1 ${INSTALL_DIR}/.venv-kicad-ipc/bin/python ${INSTALL_DIR}/docker/smoke_linux_ipc.py"
echo "2. Authenticate your subscription:"
case "$PROVIDER" in
  claude)
    echo "   # On desktop: claude setup-token, then on this VM:"
    echo "   export CLAUDE_CODE_OAUTH_TOKEN='<your-token>'"
    echo "   # Or run: claude login (via SSH port-forward if needed)"
    ;;
  codex)
    echo "   codex login --device-auth"
    echo "   codex exec --help >/dev/null && codex features list >/dev/null"
    ;;
  gemini)
    echo "   # Connect via SSH local port forward for OAuth callback, then install/sign in to agy:"
    echo "   agy --help >/dev/null"
    ;;
esac
echo "3. Initialize isolated board repo:"
echo "   mkdir -p ~/rj45-${PROVIDER} && cd ~/rj45-${PROVIDER} && git init -b main"
echo "   ${INSTALL_DIR}/.venv-kicad-ipc/bin/board-graph init --autonomous"
