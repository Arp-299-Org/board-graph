#!/usr/bin/env python3
"""Export and validate the schematic BOM CSV in the exact format required by `board-graph bom`."""
from __future__ import annotations

import argparse
import csv
import json
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import board_description  # noqa: E402
import tool_paths  # noqa: E402
from kicad_sexp import child, children, parse, prop  # noqa: E402

if __name__ == "__main__":
    tool_paths.take_argument()

ROOT = tool_paths.project_root()
BOM_COLUMNS = ("Refs", "Qty", "Value", "Manufacturer", "MPN", "Supplier", "Supplier Part")
BOM_FIELDS = (
    ("Value", "Value"),
    ("Manufacturer", "Manufacturer"),
    ("MPN", ("Manufacturer Part", "MPN")),
    ("Supplier", "Supplier"),
    ("Supplier Part", "Supplier Part"),
)


def _ref_sort_key(ref: str) -> tuple[str, int, str]:
    match = re.fullmatch(r"([A-Za-z]+)(\d+)", ref)
    return (match.group(1), int(match.group(2)), ref) if match else (ref, 0, ref)


def _is_fitted_symbol(symbol: list) -> bool:
    return child(symbol, "in_bom") != ["in_bom", "no"] and child(symbol, "dnp") != ["dnp", "yes"]


def read_bom_rows(path: pathlib.Path, bom_rel: str, columns: tuple[str, ...] = BOM_COLUMNS) -> list[dict[str, str]]:
    """Read a BOM CSV and raise ValueError naming any missing required columns."""
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        missing = [column for column in columns if column not in (reader.fieldnames or ())]
        if missing:
            raise ValueError(f"{bom_rel}: missing columns: {', '.join(missing)}")
        return list(reader)


def _pcb_mismatches(pcb_path: pathlib.Path, by_ref: dict[str, dict[str, str]]) -> list[str]:
    """Compare fitted schematic fields against saved PCB footprints and LCSC Part #."""
    import board_graph_actions

    pcb = parse(pcb_path.read_text(encoding="utf-8"))
    fps = {prop(fp, "Reference"): fp for fp in children(pcb, "footprint")}
    mismatches: list[str] = []
    board_parts: set[str] = set()
    for ref, fp in fps.items():
        if not ref:
            continue
        attr = child(fp, "attr") or []
        if any(flag in attr for flag in ("exclude_from_bom", "dnp")):
            continue
        board_parts.add(ref)
        sym = by_ref.get(ref)
        if sym is None:
            mismatches.append(f"{ref}: on board without a matching fitted schematic symbol")
            continue
        for column, field_name in BOM_FIELDS:
            actual = (prop(fp, field_name) or "").strip()
            if actual != sym[column]:
                mismatches.append(f"{ref}: board {column} ({actual or 'missing'}) != schematic ({sym[column]})")
    for ref in sorted(set(by_ref) - board_parts, key=_ref_sort_key):
        mismatches.append(f"{ref}: fitted schematic symbol missing from board or excluded")
    mismatches.extend(
        board_graph_actions.lcsc_field_findings(
            (ref, ref in board_parts and "smd" in (child(fp, "attr") or []), prop(fp, "LCSC Part #"), prop(fp, "Supplier Part"))
            for ref, fp in fps.items()
            if ref
        )
    )
    return mismatches


def export_bom(
    root: pathlib.Path,
    *,
    out: pathlib.Path | None = None,
    check_pcb: bool = False,
    board_description_path: pathlib.Path | None = None,
) -> dict:
    """Export grouped BOM CSV from the schematic and validate required purchasing properties."""
    desc = board_description.load(board_description_path, root=root) if board_description_path else board_description.load(root=root)
    sch_path = desc.path("schematic").resolve()
    out_path = (out or desc.path("bom_export")).resolve()
    sch = parse(sch_path.read_text(encoding="utf-8"))

    by_ref: dict[str, dict[str, str]] = {}
    missing_fields: list[str] = []
    for sym in children(sch, "symbol"):
        ref = (prop(sym, "Reference") or "").strip()
        if not ref or ref.startswith("#") or not _is_fitted_symbol(sym):
            continue
        values: dict[str, str] = {}
        for column, field_name in BOM_FIELDS:
            val = (prop(sym, field_name) or "").strip()
            if not val:
                label = field_name[0] if isinstance(field_name, tuple) else field_name
                missing_fields.append(f"{ref}: missing required schematic property {label!r}")
            values[column] = val
        by_ref[ref] = values

    if missing_fields:
        raise ValueError("; ".join(missing_fields))

    groups: dict[tuple[str, ...], list[str]] = {}
    for ref in sorted(by_ref, key=_ref_sort_key):
        vals = by_ref[ref]
        key = tuple(vals[col] for col, _ in BOM_FIELDS)
        groups.setdefault(key, []).append(ref)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, str]] = []
    with out_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(BOM_COLUMNS), lineterminator="\n")
        writer.writeheader()
        for key, group_refs in groups.items():
            row = {
                "Refs": ",".join(group_refs),
                "Qty": str(len(group_refs)),
                **{col: val for (col, _), val in zip(BOM_FIELDS, key)},
            }
            writer.writerow(row)
            rows.append(row)

    pcb_mismatches: list[str] = []
    if check_pcb:
        pcb_path = desc.path("pcb").resolve()
        pcb_mismatches = _pcb_mismatches(pcb_path, by_ref)

    try:
        rel_out = out_path.relative_to(pathlib.Path(root).resolve()).as_posix()
    except ValueError:
        rel_out = str(out_path)
    return {
        "out": rel_out,
        "rows": rows,
        "fitted_components": len(by_ref),
        "pcb_mismatches": pcb_mismatches,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=pathlib.Path, help="Output BOM CSV path (default from board description)")
    parser.add_argument("--check-pcb", action="store_true", help="Also compare schematic BOM fields against saved PCB")
    parser.add_argument("--json", action="store_true", help="Print JSON output")
    board_description.add_argument(parser)
    args = parser.parse_args(argv)

    try:
        report = export_bom(
            ROOT,
            out=args.out,
            check_pcb=args.check_pcb,
            board_description_path=args.board_description,
        )
    except (OSError, ValueError) as error:
        print(f"board-graph export-bom: {error}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"wrote {report['out']} ({len(report['rows'])} row(s), {report['fitted_components']} fitted part(s))")
        for item in report["pcb_mismatches"]:
            print(f"mismatch: {item}", file=sys.stderr)
    return 1 if report["pcb_mismatches"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
