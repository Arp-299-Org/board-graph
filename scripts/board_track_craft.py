#!/usr/bin/env python3
"""Run check_track_craft.py safely via the configured KiCad machine (or locally under one_machine)."""
from __future__ import annotations

import argparse
import io
import json
import os
import pathlib
import shlex
import subprocess
import sys
import tarfile

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import tool_paths  # noqa: E402

if __name__ == "__main__":
    tool_paths.take_argument()

import boards  # noqa: E402

if __name__ == "__main__":
    boards.take_argument()

from no_window import KWARGS as NO_WINDOW  # noqa: E402

TOOL_ROOT = tool_paths.TOOL_ROOT
ROOT = tool_paths.project_root()
CHECKER_REL = "tools/kicad/check_track_craft.py"
MAC_KICAD_PYTHON = "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3"
REPORT_START = "__BOARD_GRAPH_TRACK_CRAFT_REPORT_START__"
REPORT_END = "__BOARD_GRAPH_TRACK_CRAFT_REPORT_END__"


def _local_kicad_python() -> str:
    override = os.environ.get("KICAD_PYTHON")
    if override:
        return override
    sys.path.insert(0, str(TOOL_ROOT / "tools" / "kicad"))
    import kicad_install  # noqa: E402

    return str(kicad_install.KICAD_PYTHON)


def _resolve_project_file(raw: str | None, default_rel: str | None = None) -> pathlib.Path | None:
    if raw:
        candidate = pathlib.Path(raw)
        return candidate if candidate.is_absolute() else (ROOT / candidate)
    if default_rel:
        return ROOT / default_rel
    return None


def _build_stage_tar(
    board_path: pathlib.Path,
    checker_path: pathlib.Path,
    witness_path: pathlib.Path | None,
) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for arcname, src in (
            ("check_track_craft.py", checker_path),
            ("board.kicad_pcb", board_path),
        ):
            tf.add(str(src), arcname=arcname)
        if witness_path is not None:
            tf.add(str(witness_path), arcname="witnesses.json")
    return buf.getvalue()


def _extract_remote_report(stdout: str) -> tuple[str, str | None]:
    if REPORT_START not in stdout or REPORT_END not in stdout:
        return stdout, None
    before, rest = stdout.split(REPORT_START, 1)
    payload, after = rest.split(REPORT_END, 1)
    cleaned = (before.rstrip("\n") + "\n" + after.lstrip("\n")).strip()
    return (cleaned + "\n") if cleaned else "", payload.strip() + "\n"


def _run_local(
    board_path: pathlib.Path,
    checker_path: pathlib.Path,
    output_path: pathlib.Path | None,
    geometry: bool,
    witness_path: pathlib.Path | None,
    timeout: int,
) -> int:
    cmd = [_local_kicad_python(), str(checker_path), str(board_path)]
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        cmd.extend(["--json", str(output_path)])
    if geometry:
        cmd.append("--geometry")
    if witness_path is not None:
        cmd.extend(["--witnesses", str(witness_path)])
    try:
        done = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout, check=False, **NO_WINDOW)
    except subprocess.TimeoutExpired:
        print(f"track-craft timed out after {timeout}s", file=sys.stderr)
        return 124
    if done.stdout:
        sys.stdout.write(done.stdout)
    if done.stderr:
        sys.stderr.write(done.stderr)
    return done.returncode


def _run_remote(
    mac_mod,
    board_path: pathlib.Path,
    checker_path: pathlib.Path,
    output_path: pathlib.Path | None,
    geometry: bool,
    witness_path: pathlib.Path | None,
    timeout: int,
) -> int:
    tar_bytes = _build_stage_tar(board_path, checker_path, witness_path)
    extra_flags: list[str] = []
    if output_path is not None:
        extra_flags.extend(["--json", '"$tmp/report.json"'])
    if geometry:
        extra_flags.append("--geometry")
    if witness_path is not None:
        extra_flags.extend(["--witnesses", '"$tmp/witnesses.json"'])
    kicad_py = shlex.quote(MAC_KICAD_PYTHON)
    flags_str = " ".join(extra_flags)
    remote_cmd = (
        'tmp=$(mktemp -d /tmp/board-graph-tc.XXXXXX) && '
        'trap \'rm -rf "$tmp"\' EXIT && '
        'tar -xf - -C "$tmp" && '
        'rc=0 && '
        f'KICAD_PY="${{KICAD_PYTHON:-{kicad_py}}}" && '
        f'"$KICAD_PY" "$tmp/check_track_craft.py" "$tmp/board.kicad_pcb" {flags_str} || rc=$?; '
        'if [ -f "$tmp/report.json" ]; then '
        f'printf "\\n{REPORT_START}\\n"; cat "$tmp/report.json"; printf "\\n{REPORT_END}\\n"; '
        'fi; exit "$rc"'
    )
    argv = [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
        "-i", str(mac_mod.KEY), mac_mod.HOST, remote_cmd,
    ]
    try:
        done = subprocess.run(argv, input=tar_bytes, capture_output=True, timeout=timeout, check=False, **NO_WINDOW)
    except subprocess.TimeoutExpired:
        print(f"track-craft on {mac_mod.HOST} timed out after {timeout}s", file=sys.stderr)
        return 124
    stdout_text, report_payload = _extract_remote_report(done.stdout.decode("utf-8", errors="replace"))
    stderr_text = done.stderr.decode("utf-8", errors="replace")
    if stdout_text:
        sys.stdout.write(stdout_text)
    if stderr_text:
        sys.stderr.write(stderr_text)
    if output_path is not None and report_payload is not None:
        json.loads(report_payload)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report_payload, encoding="utf-8")
    return done.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run track-craft check on the configured KiCad machine")
    parser.add_argument("board_path", nargs="?", help="path to .kicad_pcb (defaults to board description pcb)")
    parser.add_argument("--json", "--output", dest="output", help="write JSON report to this path")
    parser.add_argument("--geometry", action="store_true")
    parser.add_argument("--witnesses", help="JSON witness file path")
    parser.add_argument("--timeout", type=int, default=180, help="timeout in seconds (default 180)")
    boards.add_argument(parser, default=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    import board_description  # noqa: E402
    import mac  # noqa: E402

    board_path = _resolve_project_file(args.board_path, board_description.load().rel("pcb"))
    if board_path is None or not board_path.is_file():
        print(f"Board file does not exist: {board_path}", file=sys.stderr)
        return 2
    witness_path = _resolve_project_file(args.witnesses)
    if witness_path is not None and not witness_path.is_file():
        print(f"Witness file does not exist: {witness_path}", file=sys.stderr)
        return 2
    output_path = _resolve_project_file(args.output)
    checker_path = TOOL_ROOT / CHECKER_REL

    if mac.LOCAL:
        return _run_local(board_path, checker_path, output_path, args.geometry, witness_path, args.timeout)
    return _run_remote(mac, board_path, checker_path, output_path, args.geometry, witness_path, args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
