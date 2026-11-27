#!/usr/bin/env python3
"""Generate provisional or final quote deliverables: Gerbers/drill ZIP, IPC netlist, BOM/CPL, and checksums."""
from __future__ import annotations

import argparse
import hashlib
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import board_description  # noqa: E402
import tool_paths  # noqa: E402
import validate_board  # noqa: E402

if __name__ == "__main__":
    tool_paths.take_argument()

TOOL_ROOT = tool_paths.TOOL_ROOT
ROOT = tool_paths.project_root()


def _sha256_upper(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def write_checksums(out_dir: pathlib.Path, filenames: list[str] | None = None) -> tuple[pathlib.Path, pathlib.Path]:
    """Write .gitattributes (preserving CRLF CSVs and binary ZIPs) and SHA256SUMS.txt in `out_dir`."""
    out_dir.mkdir(parents=True, exist_ok=True)
    attrs = out_dir / ".gitattributes"
    attrs.write_text("*.csv -text !eol\n*.zip -text\n", encoding="utf-8", newline="\n")
    names = sorted(filenames) if filenames is not None else sorted(
        p.name for p in out_dir.iterdir()
        if p.is_file() and p.name not in (".gitattributes", "SHA256SUMS.txt")
    )
    lines = [f"{_sha256_upper(out_dir / name)}  {name}" for name in names if (out_dir / name).is_file()]
    sums = out_dir / "SHA256SUMS.txt"
    sums.write_text(("\n".join(lines) + "\n") if lines else "", encoding="utf-8", newline="\n")
    return attrs, sums


def _run_tool(script_name: str, args: list[str]) -> None:
    done = subprocess.run([sys.executable, "-B", str(TOOL_ROOT / "scripts" / script_name), *args],
                          cwd=str(ROOT), capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise SystemExit(f"{script_name} failed:\n{done.stdout}\n{done.stderr}".strip())
    if done.stdout.strip():
        print(done.stdout.strip())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=pathlib.Path, help="Output folder (default: board/reports/quote_files/provisional)")
    parser.add_argument("--base-name", help="Deliverable filename prefix (default: <package_name>_R1)")
    parser.add_argument("--board", type=pathlib.Path, help="Board .kicad_pcb path")
    parser.add_argument("--skip-jlc", action="store_true", help="Skip offline pcbnew BOM/CPL generation")
    board_description.add_argument(parser)
    args = parser.parse_args(argv)

    desc = board_description.load(args.board_description, root=ROOT) if args.board_description else board_description.load(root=ROOT)
    board = (args.board or desc.path("pcb")).resolve()
    out_dir = (args.out or (desc.path("reports") / "quote_files" / "provisional")).resolve()
    base_name = args.base_name or f"{desc['package_name']}_R1"
    out_dir.mkdir(parents=True, exist_ok=True)

    _run_tool("kicad_generate_fabrication_archive.py", [
        "--board", str(board), "--production", str(out_dir), "--base-name", base_name,
    ])
    if not args.skip_jlc:
        jlc_args = ["--board", str(board), "--production", str(out_dir), "--base-name", base_name]
        for script in ("scripts/kicad_generate_jlc_source_files.py", "scripts/kicad_finalize_jlc_outputs.py"):
            res = validate_board.kicad_offline(script, *jlc_args)
            if res.returncode != 0:
                raise SystemExit(f"{script} failed:\n{res.stdout}\n{res.stderr}".strip())
            if res.stdout.strip():
                print(res.stdout.strip())

    _, sums = write_checksums(out_dir)
    print(f"wrote {sums}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
