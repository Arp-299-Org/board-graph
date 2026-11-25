#!/usr/bin/env python3
"""Audit lifecycle enforcement: generate, handle, dispose.

A dated report is evidence about one exact board state. The moment the board
changes, that evidence is void, but the file keeps sitting there looking
authoritative. That is how a stale audit gets cited as current truth, and how a
manufacturing package can be certified against gates that were run on a
different board.

This tool closes that gap by binding every report to the SHA-256 of the sources
it describes:

  stamp    record the current board/schematic hashes for a report just produced
  check    classify every report as fresh, stale, or unknown, and fail if a
           required report is not fresh
  dispose  move non-fresh reports into reports/superseded/<date>/ so nothing
           can mistake them for current evidence

A report with no provenance entry is UNKNOWN, which counts as not fresh. That
default is deliberate: unverifiable evidence gets regenerated, never trusted.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import pathlib
import shutil
import sys

PROVENANCE_NAME = "provenance.json"


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def source_hashes(board: pathlib.Path) -> dict[str, str]:
    hashes = {"board_sha256": sha256(board)}
    schematic = board.with_suffix(".kicad_sch")
    if schematic.is_file():
        hashes["schematic_sha256"] = sha256(schematic)
    return hashes


def load_provenance(reports: pathlib.Path) -> dict:
    path = reports / PROVENANCE_NAME
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_provenance(reports: pathlib.Path, data: dict) -> None:
    (reports / PROVENANCE_NAME).write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def self_declared_date(path: pathlib.Path) -> datetime.datetime | None:
    """The date a report claims for itself, if it carries one.

    KiCad's JSON reports record when they were generated. That is the one piece
    of evidence a stamp cannot fake, because it comes from the tool that produced
    the file rather than from whoever is stamping it.
    """
    if path.suffix.lower() != ".json":
        return None
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
        value = report.get("date") if isinstance(report, dict) else None
        return datetime.datetime.fromisoformat(value) if value else None
    except (json.JSONDecodeError, ValueError, OSError):
        return None


def refuse_stale_stamp(path: pathlib.Path, board: pathlib.Path) -> str | None:
    """Why this file must not be stamped against this board, or None.

    A stamp is an assertion by whoever runs it, and an assertion made in a loop
    over a list of filenames is not an inspection. This is what makes the stamp
    mean something: a report that says it was generated before the board was last
    saved cannot possibly describe it, so refuse regardless of intent.

    Paid for on 2026-07-25, when a July 12 DRC report was batch-stamped against a
    July 25 board with a generator string about reviewing 3D renders. The stamp
    made a stale file look fresh, and the safeguard became the vector.
    """
    board_mtime = datetime.datetime.fromtimestamp(board.stat().st_mtime)
    declared = self_declared_date(path)
    if declared is not None and declared < board_mtime:
        return (f"{path.name} says it was generated {declared.isoformat(timespec='seconds')}, "
                f"before the board was last saved "
                f"{board_mtime.isoformat(timespec='seconds')}. Regenerate it; "
                f"stamping cannot make it describe this board.")
    file_mtime = datetime.datetime.fromtimestamp(path.stat().st_mtime)
    if file_mtime < board_mtime - datetime.timedelta(seconds=1):
        return (f"{path.name} was last written {file_mtime.isoformat(timespec='seconds')}, "
                f"before the board at {board_mtime.isoformat(timespec='seconds')}. "
                f"Regenerate it rather than stamping it.")
    return None


def classify(reports: pathlib.Path, board: pathlib.Path) -> dict[str, str]:
    """Return {report filename: fresh|stale|unknown} for every report present."""
    current = source_hashes(board)
    provenance = load_provenance(reports)
    verdicts: dict[str, str] = {}
    for path in sorted(reports.iterdir()):
        if not path.is_file() or path.name == PROVENANCE_NAME:
            continue
        if path.suffix.lower() not in {".json", ".md"}:
            continue
        record = provenance.get(path.name)
        if not record:
            verdicts[path.name] = "unknown"
            continue
        matches = record.get("board_sha256") == current["board_sha256"]
        if "schematic_sha256" in current and "schematic_sha256" in record:
            matches = matches and record["schematic_sha256"] == current["schematic_sha256"]
        verdicts[path.name] = "fresh" if matches else "stale"
    return verdicts


def require_fresh(reports: pathlib.Path, board: pathlib.Path, required: list[str]) -> None:
    """Raise if any required report does not describe the current sources.

    Intended for callers that turn reports into claims, above all the
    manufacturing packager.
    """
    verdicts = classify(reports, board)
    problems = []
    for name in required:
        verdict = verdicts.get(name, "missing")
        if verdict != "fresh":
            problems.append(f"{name}: {verdict}")
    if problems:
        raise RuntimeError(
            "Refusing to consume reports that do not describe the current board "
            f"({board.name}):\n  " + "\n  ".join(problems) +
            "\nRegenerate the gates against this board and stamp them with "
            "scripts/kicad_report_freshness.py stamp."
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["stamp", "check", "dispose"])
    parser.add_argument("--reports", type=pathlib.Path, required=True)
    parser.add_argument("--board", type=pathlib.Path, required=True)
    parser.add_argument("--report", action="append", default=[],
                        help="stamp: report filename to bind to the current sources")
    parser.add_argument("--generator", default="",
                        help="stamp: command that produced the report, for the record")
    parser.add_argument("--require", action="append", default=[],
                        help="check: report that must be fresh, else exit non-zero")
    args = parser.parse_args()

    reports = args.reports.resolve()
    board = args.board.resolve()
    if not board.is_file():
        print(f"No such board: {board}", file=sys.stderr)
        return 2

    if args.action == "stamp":
        if not args.report:
            print("stamp needs at least one --report", file=sys.stderr)
            return 2
        provenance = load_provenance(reports)
        stamped_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        refusals = []
        for name in args.report:
            if not (reports / name).is_file():
                print(f"No such report: {reports / name}", file=sys.stderr)
                return 2
            reason = refuse_stale_stamp(reports / name, board)
            if reason:
                refusals.append(reason)
        if refusals:
            print("Refusing to stamp:\n  " + "\n  ".join(refusals), file=sys.stderr)
            return 2
        for name in args.report:
            provenance[name] = {
                **source_hashes(board),
                "board": board.name,
                "generated_utc": stamped_at,
                "generator": args.generator,
            }
        save_provenance(reports, provenance)
        print(f"stamped {len(args.report)} report(s) against {board.name}")
        return 0

    verdicts = classify(reports, board)
    if args.action == "check":
        for name, verdict in verdicts.items():
            print(f"{verdict:8} {name}")
        stale = [n for n, v in verdicts.items() if v != "fresh"]
        for name in args.require:
            if verdicts.get(name, "missing") != "fresh":
                print(f"\nFAIL: required report not fresh: {name} "
                      f"({verdicts.get(name, 'missing')})", file=sys.stderr)
                return 1
        print(f"\n{len(verdicts) - len(stale)} fresh, {len(stale)} not fresh")
        return 0

    # dispose
    # Only reports that were stamped and no longer match are disposed. An
    # unstamped report is not necessarily stale evidence: narrative documents,
    # inspection records, and anything predating this system live here too, and
    # some are inputs the packager requires. Unknown gets reported, never moved.
    stale = [name for name, verdict in verdicts.items() if verdict == "stale"]
    unknown = [name for name, verdict in verdicts.items() if verdict == "unknown"]
    if unknown:
        print("not stamped, left in place (stamp them or treat as narrative):")
        for name in unknown:
            print(f"   {name}")
    if not stale:
        print("nothing to dispose: no stamped report describes a superseded board")
        return 0
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    destination = reports / "superseded" / stamp
    destination.mkdir(parents=True, exist_ok=True)
    provenance = load_provenance(reports)
    for name in stale:
        shutil.move(str(reports / name), str(destination / name))
        provenance.pop(name, None)
    save_provenance(reports, provenance)
    (destination / "WHY_SUPERSEDED.md").write_text(
        f"""# Superseded reports, disposed {stamp}

These reports describe a board state that no longer exists. They were moved out
of the project reports directory so they cannot be read as current evidence.

Current board at disposal time: `{board.name}`
SHA-256: `{source_hashes(board)['board_sha256']}`

Disposed: {', '.join(sorted(stale))}

Point-in-time observations only. Verify against the board or the code before
citing anything here.
""",
        encoding="utf-8",
    )
    print(f"disposed {len(stale)} report(s) to {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
