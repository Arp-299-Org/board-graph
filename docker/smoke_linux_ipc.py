#!/usr/bin/env python3
"""Live KiCad 10 + Xvfb + Mode: ipc smoke check inside the Linux Docker runner."""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

TOOL_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS_DIR = TOOL_ROOT / "scripts"
KICAD_TOOLS_DIR = TOOL_ROOT / "tools" / "kicad"
for _p in (SCRIPTS_DIR, KICAD_TOOLS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

def _dump_failure_diagnostics(pcb_path: pathlib.Path, pcbnew_bin: pathlib.Path) -> None:
    from kipy import KiCad

    print("=== DOCKER SMOKE DIAGNOSTICS ===", file=sys.stderr, flush=True)
    for label, cmd in (
        ("kicad-cli --version", ["kicad-cli", "--version"]),
        ("ls -la /tmp/kicad", ["ls", "-la", "/tmp/kicad"]),
        ("ls -la /root/.config/kicad/10.0", ["ls", "-la", "/root/.config/kicad/10.0"]),
        ("pgrep -af pcbnew (before cleanup)", ["pgrep", "-af", "pcbnew"]),
    ):
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        print(f"$ {label} (rc={res.returncode})\n{res.stdout}{res.stderr}", file=sys.stderr, flush=True)

    for pattern in ("api*.sock", "api*.lock"):
        for sock_file in pathlib.Path("/tmp/kicad").glob(pattern):
            try:
                sock_file.unlink()
            except OSError as exc:
                print(f"unlink {sock_file}: {exc!r}", file=sys.stderr, flush=True)

    probe = subprocess.Popen(
        [str(pcbnew_bin), str(pcb_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        time.sleep(10)
        print(f"$ probe.poll()={probe.poll()}", file=sys.stderr, flush=True)
        res = subprocess.run(["ls", "-la", "/tmp/kicad"], capture_output=True, text=True, check=False)
        print(f"$ ls -la /tmp/kicad (during probe, rc={res.returncode})\n{res.stdout}{res.stderr}", file=sys.stderr, flush=True)

        wids = subprocess.run(["xdotool", "search", "--name", "."], capture_output=True, text=True, check=False)
        print(f"$ xdotool search --name . (rc={wids.returncode})\n{wids.stdout}{wids.stderr}", file=sys.stderr, flush=True)
        for wid in [line.strip() for line in wids.stdout.splitlines() if line.strip()]:
            wname = subprocess.run(["xdotool", "getwindowname", wid], capture_output=True, text=True, check=False)
            print(f"  wid={wid} title={wname.stdout.strip()!r}", file=sys.stderr, flush=True)

        mscreen = subprocess.run([str(SCRIPTS_DIR / "mac_screen.sh"), "list-json"], capture_output=True, text=True, check=False)
        print(f"$ mac_screen.sh list-json (rc={mscreen.returncode})\n{mscreen.stdout}{mscreen.stderr}", file=sys.stderr, flush=True)

        for sock_file in sorted(pathlib.Path("/tmp/kicad").glob("api*.sock")):
            uri = f"ipc://{sock_file}"
            try:
                ver = KiCad(socket_path=uri, timeout_ms=5000).get_version()
                print(f"kipy get_version({uri}) -> {ver!r}", file=sys.stderr, flush=True)
            except Exception as exc:
                print(f"kipy get_version({uri}) EXC -> {exc!r}", file=sys.stderr, flush=True)
            try:
                brd = KiCad(socket_path=uri, timeout_ms=5000).get_board()
                print(
                    f"kipy get_board({uri}) -> ({brd.document.project.path!r}, {brd.document.board_filename!r})",
                    file=sys.stderr,
                    flush=True,
                )
            except Exception as exc:
                print(f"kipy get_board({uri}) EXC -> {exc!r}", file=sys.stderr, flush=True)
    finally:
        if probe.poll() is None:
            probe.terminate()
            try:
                out, err = probe.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                probe.kill()
                out, err = probe.communicate()
        else:
            out, err = probe.communicate()
        print(f"$ probe exit={probe.returncode}\nstdout:\n{out}\nstderr:\n{err}", file=sys.stderr, flush=True)


def _verify_saved_board_contracts(
    pcb_path: pathlib.Path,
    before_placement: str,
    root: pathlib.Path,
    run_kicad_cli,
) -> dict:
    import board_graph_actions
    import kicad_sexp

    saved_text = pcb_path.read_text(encoding="utf-8")
    tree = kicad_sexp.parse(saved_text)
    thermal_gap = None
    thermal_bridge = None
    for item in tree[1:]:
        if not isinstance(item, list) or not item or item[0] != "zone":
            continue
        net_node = kicad_sexp.child(item, "net_name") or kicad_sexp.child(item, "net")
        if kicad_sexp.child(item, "layer") != ["layer", "B.Cu"] or not (net_node and net_node[-1] == "GND"):
            continue
        fill_node = kicad_sexp.child(item, "fill") or []
        for sub in [*item[1:], *fill_node[1:]]:
            if isinstance(sub, list) and len(sub) >= 2:
                if sub[0] == "thermal_gap":
                    thermal_gap = float(sub[1])
                elif sub[0] == "thermal_bridge_width":
                    thermal_bridge = float(sub[1])
    if thermal_gap != 0.5 or thermal_bridge != 0.5:
        raise RuntimeError(f"Expected B.Cu GND thermal spokes 0.5 mm, got gap={thermal_gap!r}, bridge={thermal_bridge!r}")

    after_placement = board_graph_actions.placement_view(saved_text)
    if after_placement != before_placement:
        raise RuntimeError(f"placement_view drifted across save: {before_placement[:12]} -> {after_placement[:12]}")

    gerber_dir = root / "gerbers-smoke"
    gerber_dir.mkdir(parents=True, exist_ok=True)
    gerb = run_kicad_cli(
        ["pcb", "export", "gerbers", "--output", str(gerber_dir), str(pcb_path)],
        capture_output=True,
        text=True,
    )
    gbr_files = [p for p in gerber_dir.glob("*") if p.is_file() and p.stat().st_size > 0]
    if gerb.returncode != 0 or len(gbr_files) < 3:
        raise RuntimeError(f"kicad-cli gerber export failed ({gerb.returncode}, files={len(gbr_files)}): {gerb.stderr or gerb.stdout}")
    return {
        "pour_thermal_verified": True,
        "placement_hash_stable": True,
        "gerber_files": len(gbr_files),
    }


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="bg-linux-smoke-") as td:
        root = pathlib.Path(td).resolve()
        site_dir = root / "tools"
        site_dir.mkdir(parents=True)
        site_file = site_dir / "board_graph_site.json"
        site_file.write_text(
            json.dumps({"one_machine": True, "state_database": "logs/board-graph/state.sqlite3"}) + "\n",
            encoding="utf-8",
        )
        os.environ["BOARD_GRAPH_SITE"] = str(site_file)
        os.environ["BOARD_GRAPH_PROJECT"] = str(root)

        proj_dir = root / "board"
        proj_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(TOOL_ROOT / "board" / "board.json", proj_dir / "board.json")

        import board_graph_actions
        import editor_step_kicad
        import kicad_install
        import kicad_ipc
        import kicad_lay
        import kicad_reload_board
        import test_dense_board_integration as dense
        from invoke_kicad_cli import run_kicad_cli

        subprocess.run(["git", "init", "-q"], cwd=str(root), check=True)
        bundle = dense.build_dense_board_bundle(root)
        pcb_path = bundle["pcb_path"].resolve()
        pro_path = bundle["pro_path"].resolve()

        drc_out = root / "drc-smoke.json"
        drc = run_kicad_cli(
            ["pcb", "drc", "--refill-zones", "--severity-all", "--format", "json", "--output", str(drc_out), str(pcb_path)],
            capture_output=True,
            text=True,
        )
        if drc.returncode != 0 or not drc_out.is_file():
            raise RuntimeError(f"kicad-cli drc failed ({drc.returncode}): {drc.stderr or drc.stdout}")
        drc_data = json.loads(drc_out.read_text(encoding="utf-8"))

        editor_step_kicad.refuse_if_editor_running()
        try:
            proc, socket = editor_step_kicad.open_editor(pcb_path)
        except BaseException:
            _dump_failure_diagnostics(pcb_path, kicad_install.KICAD_PCBNEW)
            raise
        try:
            connected = kicad_ipc.connect_board(socket_path=socket, expected_project=pro_path)
            titles = kicad_reload_board.editor_titles(proc.pid)
            idle_reason = editor_step_kicad.not_idle(proc, socket, pcb_path)
            if idle_reason:
                raise RuntimeError(f"not_idle returned: {idle_reason} (titles={titles})")

            editor_step_kicad.lock(pcb_path, True)
            if not editor_step_kicad.is_locked(pcb_path):
                raise RuntimeError("Linux chmod lock failed")
            editor_step_kicad.lock(pcb_path, False)
            if editor_step_kicad.is_locked(pcb_path):
                raise RuntimeError("Linux chmod unlock failed")

            pic_path = root / "editor-capture.png"
            editor_step_kicad.picture_of_editor(proc.pid, pic_path, pcb_path.stem)
            if not pic_path.is_file() or pic_path.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":
                raise RuntimeError("picture_of_editor did not produce a valid PNG")

            look_png = root / "mac-look.png"
            look_done = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "mac.py"), "look", "PCB Editor", "--out", str(look_png)],
                capture_output=True,
                text=True,
                check=False,
            )
            if look_done.returncode != 0 or not look_png.is_file() or look_png.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":
                raise RuntimeError(f"mac.py look failed ({look_done.returncode}): {look_done.stderr or look_done.stdout}")

            os.environ["KICAD_API_SOCKET"] = socket
            board = kicad_lay.KipyLayBoard(pid=str(proc.pid))
            board.wait_ready()
            board.board.save()
            before_placement = board_graph_actions.placement_view(pcb_path.read_text(encoding="utf-8"))

            routes = [
                {"from": "Q1.1", "to": "Q1.2", "layer": "F.Cu", "steps": []},
                {"from": "Q2.1", "to": "Q2.2", "layer": "F.Cu", "steps": []},
                {"from": "Q3.1", "to": "Q3.2", "layer": "F.Cu", "steps": []},
            ]
            pours = [{"net": "GND", "layer": "B.Cu", "rect": [84, 46, 120, 80], "connect": "thermal"}]
            batch = kicad_lay.execute_batch(board, {"routes": routes, "pours": pours}, run_drc=True)
            board.board.save()
            contracts = _verify_saved_board_contracts(pcb_path, before_placement, root, run_kicad_cli)
            print(json.dumps({
                "ok": True,
                "socket": socket,
                "board_name": connected.document.board_filename,
                "titles": titles,
                "drc_violations": len(drc_data.get("violations", [])),
                "tracks_created": batch["tracks_created"],
                "editor_png_bytes": pic_path.stat().st_size,
                "mac_look_png_bytes": look_png.stat().st_size,
                **contracts,
            }))
        finally:
            editor_step_kicad.close_editor(proc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
