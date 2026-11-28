#!/usr/bin/env python3
"""Run verification checks, refresh exclusions, and stamp every report against the current board hash."""
from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import board_description  # noqa: E402
import board_graph_actions  # noqa: E402
import kicad_report_freshness  # noqa: E402
import tool_paths  # noqa: E402
from no_window import KWARGS as NO_WINDOW  # noqa: E402

if __name__ == "__main__":
    tool_paths.take_argument()

TOOL_ROOT = tool_paths.TOOL_ROOT
ROOT = tool_paths.project_root()


def _refresh_exclusions(exclusions_path: pathlib.Path) -> None:
    """Ensure exclusions JSON exists and has a current mtime so stamping succeeds cleanly."""
    if exclusions_path.is_file():
        data = json.loads(exclusions_path.read_text(encoding="utf-8"))
    else:
        data = {"accepted_warning_refs": []}
    exclusions_path.parent.mkdir(parents=True, exist_ok=True)
    exclusions_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _stamp_folder(folder: pathlib.Path, names: list[str], board_path: pathlib.Path, generator: str, stamped_at: str) -> list[str]:
    hashes = kicad_report_freshness.source_hashes(board_path)
    provenance = kicad_report_freshness.load_provenance(folder)
    stamped: list[str] = []
    for name in names:
        report = folder / name
        reason = kicad_report_freshness.refuse_stale_stamp(report, board_path)
        if reason:
            raise ValueError(reason)
        provenance[name] = {
            **hashes,
            "board": board_path.name,
            "generated_utc": stamped_at,
            "generator": generator,
        }
        stamped.append(name)
    kicad_report_freshness.save_provenance(folder, provenance)
    return stamped


def stamp_verification_outputs(
    checks_dir: pathlib.Path,
    exclusions_path: pathlib.Path,
    board_path: pathlib.Path,
    generator: str = "board-graph verify-prepare",
) -> dict:
    """Refresh `exclusions_path` and stamp every report under `checks_dir` plus `exclusions_path`."""
    if not board_path.is_file():
        raise ValueError(f"missing board file: {board_path}")
    if not checks_dir.is_dir():
        raise ValueError(f"missing checks folder: {checks_dir}")
    _refresh_exclusions(exclusions_path)
    report_names = sorted(
        p.name
        for p in checks_dir.iterdir()
        if p.is_file() and p.name != kicad_report_freshness.PROVENANCE_NAME and p.suffix.lower() in (".json", ".md")
    )
    if not report_names:
        raise ValueError(f"no check reports found in {checks_dir}")
    stamped_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    stamped_checks = _stamp_folder(checks_dir, report_names, board_path, generator, stamped_at)
    _stamp_folder(exclusions_path.parent, [exclusions_path.name], board_path, generator, stamped_at)
    hashes = kicad_report_freshness.source_hashes(board_path)
    return {
        **hashes,
        "stamped_checks": stamped_checks,
        "exclusions_stamped": exclusions_path.name,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=pathlib.Path, help="Checks folder (default <reports>/verification/checks)")
    parser.add_argument("--skip-validate", action="store_true", help="Only stamp existing check reports and exclusions")
    parser.add_argument("--json", action="store_true", help="Print JSON summary")
    board_description.add_argument(parser)
    args = parser.parse_args(argv)

    desc = board_description.load(args.board_description, root=ROOT) if args.board_description else board_description.load(root=ROOT)
    board_path = desc.path("pcb").resolve()
    checks_dir = (args.out or (desc.path("reports") / "verification" / "checks")).resolve()
    exclusions_path = desc.path("drc_exclusions").resolve()

    validate_rc = 0
    if not args.skip_validate:
        cmd = [sys.executable, "-B", str(TOOL_ROOT / "scripts" / "validate_board.py"), "--out", str(checks_dir)]
        if args.board_description:
            cmd += ["--board-description", str(args.board_description)]
        done = subprocess.run(cmd, cwd=str(ROOT), check=False, **NO_WINDOW)
        validate_rc = done.returncode

    try:
        stamped = stamp_verification_outputs(checks_dir, exclusions_path, board_path)
    except (OSError, ValueError) as error:
        print(f"board-graph verify-prepare: {error}", file=sys.stderr)
        return 2

    fresh = board_graph_actions.freshness(ROOT)
    result = {**stamped, "validate_exit": validate_rc, "freshness_findings": fresh["findings"]}
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(
            f"verify-prepare: board={result['board_sha256'][:12]} stamped={len(result['stamped_checks'])} "
            f"validate_exit={validate_rc} freshness_findings={len(result['freshness_findings'])}"
        )
        for item in result["freshness_findings"]:
            print(f"finding: {item}", file=sys.stderr)
    return 0 if (validate_rc == 0 and not result["freshness_findings"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
