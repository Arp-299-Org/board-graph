#!/usr/bin/env python3
"""Create and validate the SMT-only JLCPCB BOM/CPL and fabrication archive, and the hand-assembly list, whose
per-part notes come from the board's description (scripts/board_description.py, its hand_solder_notes)."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import hashlib
import pathlib
import re
import sys
import zipfile

try:
    import pcbnew
except ImportError:
    pcbnew = None

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import board_description  # noqa: E402
from kicad_generate_fabrication_archive import (  # noqa: E402
    board_copper,
    board_extra_layers,
    fabrication_extensions,
    fabrication_member_suffixes,
)

BOM_FIELDS = ["Designator", "Footprint", "Quantity", "Value", "LCSC Part #"]
CPL_FIELDS = ["Designator", "Mid X", "Mid Y", "Rotation", "Layer"]
COLOR_TOKENS = frozenset({"red", "green", "blue", "yellow", "amber", "white", "orange", "purple", "cyan", "uv", "ir"})


def read_csv_with_fields(
    path: pathlib.Path, default_fields: list[str] | None = None
) -> tuple[list[dict[str, str]], list[str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames) if reader.fieldnames else list(default_fields or [])
        return list(reader), fields


def read_csv(path: pathlib.Path) -> list[dict[str, str]]:
    return read_csv_with_fields(path)[0]


def is_functional_label(value: str) -> bool:
    """True for non-numeric functional labels (e.g. PWR, RUN) rather than numeric part ratings/values."""
    text = value.strip()
    return bool(text) and any(ch.isalpha() for ch in text) and not any(ch.isdigit() for ch in text)


def _color_tokens(value: str) -> set[str]:
    return {tok for tok in re.findall(r"[a-z]+", value.lower()) if tok in COLOR_TOKENS}


def group_smt_bom_rows(rows: list[dict[str, str]]) -> tuple[list[dict[str, str]], dict[str, list[str]]]:
    """Group SMT BOM rows by LCSC Part #, joining functional-only Value labels while rejecting electrical/color mismatches."""
    grouped: dict[str, dict[str, object]] = {}
    order: list[str] = []
    for row in rows:
        key = row["LCSC Part #"].strip() or f"__no_part__{row['Designator']}"
        refs = [part.strip() for part in row["Designator"].split(",") if part.strip()]
        value = row["Value"]
        if key not in grouped:
            entry = dict(row)
            entry["Designator"] = refs
            entry["_values"] = [value]
            grouped[key] = entry
            order.append(key)
        else:
            values: list[str] = grouped[key]["_values"]  # type: ignore[assignment]
            if value not in values:
                candidate_values = [*values, value]
                colors = set().union(*(_color_tokens(item) for item in candidate_values))
                if not all(is_functional_label(item) for item in candidate_values) or len(colors) > 1:
                    raise RuntimeError(f"{key} is used with two values: {values[0]!r} and {value!r}")
                values.append(value)
            grouped[key]["Designator"] += refs  # type: ignore[operator]

    result: list[dict[str, str]] = []
    grouped_labels: dict[str, list[str]] = {}
    for key in order:
        entry = grouped[key]
        values: list[str] = entry.pop("_values")  # type: ignore[assignment]
        refs = sorted(
            entry["Designator"],  # type: ignore[arg-type]
            key=lambda r: (re.sub(r"\d+", "", r), int(re.sub(r"\D", "", r) or 0)),
        )
        out: dict[str, str] = {k: str(v) for k, v in entry.items()}
        if len(refs) > 1:
            out["Footprint"] = out["Footprint"].split("__")[0]
        if len(values) > 1:
            grouped_labels[key] = list(values)
        out["Value"] = " / ".join(values)
        out["Designator"] = ", ".join(refs)
        out["Quantity"] = str(len(refs))
        result.append(out)
    return result, grouped_labels


def write_csv(path: pathlib.Path, rows: list[dict[str, str]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def angular_difference(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


def jlc_rotation(kicad_angle: float, top: bool) -> float:
    """CPL angle JLCPCB needs: KiCad's on the front, mirrored (180 - a) on the back.

    Same rule as kicad_generate_jlc_source_files.py: back-side footprints require
    `(180 - KiCad angle)` so polarized leads and pin-1 markers orient properly in
    JLCPCB's placement viewer across 0 and 180 degree rotations.
    """
    angle = kicad_angle if top else 180.0 - kicad_angle
    angle = (angle + 180.0) % 360.0 - 180.0
    if math.isclose(angle, -180.0, abs_tol=1e-9):
        angle = 180.0
    return round(angle, 4) + 0.0


def validate_positions(
    rows: list[dict[str, str]], footprints: dict[str, pcbnew.FOOTPRINT]
) -> list[str]:
    errors: list[str] = []
    row_by_ref: dict[str, dict[str, str]] = {}
    for row in rows:
        reference = row.get("Designator", "").strip()
        if not reference:
            errors.append("Placement row has no Designator")
        elif reference in row_by_ref:
            errors.append(f"Duplicate placement row: {reference}")
        else:
            row_by_ref[reference] = row

    if set(row_by_ref) != set(footprints):
        errors.append(
            "Full board/CPL mismatch: "
            f"board_only={sorted(set(footprints)-set(row_by_ref))} "
            f"cpl_only={sorted(set(row_by_ref)-set(footprints))}"
        )

    for reference in sorted(set(row_by_ref) & set(footprints)):
        row = row_by_ref[reference]
        footprint = footprints[reference]
        position = footprint.GetPosition()
        top = footprint.GetLayer() == pcbnew.F_Cu
        angle = footprint.GetOrientationDegrees()
        expected = {
            "x": position.x / 1_000_000.0,
            "y": -position.y / 1_000_000.0,
            "rotation": jlc_rotation(angle, top),
            "layer": "top" if top else "bottom",
        }
        try:
            actual_x = float(row["Mid X"])
            actual_y = float(row["Mid Y"])
            actual_rotation = float(row["Rotation"])
            actual_layer = row["Layer"].strip().lower()
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"Invalid placement values for {reference}: {exc}")
            continue
        if not math.isclose(actual_x, expected["x"], abs_tol=0.000002):
            errors.append(
                f"{reference} X mismatch: CPL={actual_x:.6f}, "
                f"board_absolute={expected['x']:.6f}"
            )
        if not math.isclose(actual_y, expected["y"], abs_tol=0.000002):
            errors.append(
                f"{reference} Y mismatch: CPL={actual_y:.6f}, "
                f"board_absolute={expected['y']:.6f}"
            )
        if angular_difference(actual_rotation, expected["rotation"]) > 0.001:
            errors.append(
                f"{reference} rotation mismatch: CPL={actual_rotation:.4f}, "
                f"expected={expected['rotation']:.4f}"
            )
        if actual_layer != expected["layer"]:
            errors.append(
                f"{reference} layer mismatch: CPL={actual_layer}, "
                f"expected={expected['layer']}"
            )
    return errors


def validate_fabrication_archive(
    archive_path: pathlib.Path,
    board: pcbnew.BOARD,
    copper: list[str],
    extra_layers: list[str] | tuple[str, ...] = (),
) -> tuple[list[str], dict[str, int]]:
    """`copper`: the board's copper layers in stack order, read from its saved file (board_copper), so the archive
    must hold a Gerber for each one the board has, two, four or more, and for no other count."""
    if pcbnew is None:
        raise RuntimeError("pcbnew is required to validate fabrication archives")
    errors: list[str] = []
    paste_flashes: dict[str, int] = {}
    if not archive_path.is_file():
        return [f"Missing fabrication archive: {archive_path}"], paste_flashes

    required_extensions = fabrication_extensions(copper)
    with zipfile.ZipFile(archive_path) as archive:
        members = [info for info in archive.infolist() if not info.is_dir()]
        names = [pathlib.PurePosixPath(info.filename).name.lower() for info in members]
        extensions = {pathlib.PurePosixPath(name).suffix for name in names}
        missing_extensions = sorted(required_extensions - extensions)
        if missing_extensions:
            errors.append(f"Fabrication archive missing layers: {missing_extensions}")
        for suffix in sorted(fabrication_member_suffixes(extra_layers)):
            if not any(name.endswith(suffix) for name in names):
                errors.append(f"Fabrication archive missing required layer file (*{suffix})")
        other_copper = sorted(extension for extension in extensions - required_extensions
                              if re.fullmatch(r"\.g\d+", extension))
        if other_copper:
            errors.append(f"Fabrication archive holds copper layers the board does not have: {other_copper}")
        if not any(name.endswith("-pth.drl") for name in names):
            errors.append("Fabrication archive missing plated-hole drill file")
        if not any(name.endswith("-npth.drl") for name in names):
            errors.append("Fabrication archive missing non-plated-hole drill file")

        def pasted(pad, layer) -> bool:
            # SMD pads and paste-bearing copper rings on plain holes can flash.
            if not pad.IsOnLayer(layer):
                return False
            if pad.GetAttribute() == pcbnew.PAD_ATTRIB_SMD:
                return True
            return (pad.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH
                    and pad.GetSize().x > pad.GetDrillSize().x)

        expected_paste = {
            ".gtp": sum(1 for footprint in board.GetFootprints() for pad in footprint.Pads() if pasted(pad, pcbnew.F_Paste)),
            ".gbp": sum(1 for footprint in board.GetFootprints() for pad in footprint.Pads() if pasted(pad, pcbnew.B_Paste)),
        }
        for extension, expected_count in expected_paste.items():
            matching = [
                info
                for info in members
                if pathlib.PurePosixPath(info.filename).suffix.lower() == extension
            ]
            if len(matching) != 1:
                errors.append(
                    f"Expected exactly one {extension} file, found {len(matching)}"
                )
                continue
            flash_count = archive.read(matching[0]).count(b"D03*")
            paste_flashes[extension] = flash_count
            if flash_count != expected_count:
                errors.append(
                    f"{extension} paste flash mismatch: archive={flash_count}, "
                    f"board_pads={expected_count}"
                )
    return errors, paste_flashes


def smt_manual_through_hole(footprints, smt_refs):
    work = []
    for ref in sorted(smt_refs):
        pads = [str(p.GetNumber()) for p in footprints[ref].Pads()
                if p.GetAttribute() == pcbnew.PAD_ATTRIB_PTH]
        if pads:
            work.append({'ref': ref, 'pad_numbers': pads, 'physical_joints_per_board': len(pads)})
    return work


def hand_solder_document(base_name, footprints, manual_refs, smt_refs, production_dnp, smt_manual_work, notes=None):
    """The hand-assembly list. `notes` is the description's hand_solder_notes (the default description's when it is
    not given): `manual`, a note per hand-placed part; `factory_parts`, a factory part whose through-hole joints the
    owner solders, matched by ref, part and pads, with its own note and words after its pads; `when_unpopulated` and
    `when_factory`, a line added when every one of its refs is left unpopulated, or placed by the factory."""
    if notes is None:
        notes = board_description.load()['hand_solder_notes']
    factory_notes = {item['ref']: item for item in notes['factory_parts']}
    hand = [f'# Hand assembly, {base_name}', '',
            f'Generated from the board: {len(manual_refs)} enabled manual parts; {len(smt_refs)} factory parts.', '',
            '| Ref | Face | Part | Pads | Assembly note |', '|---|---|---|---|---|']
    smt_work_by_ref = {item['ref']: item for item in smt_manual_work}
    for ref in sorted(manual_refs | set(smt_work_by_ref)):
        fp = footprints[ref]
        fields = {f.GetName(): f.GetText() for f in fp.GetFields()}
        part = fields.get('Manufacturer Part') or fp.GetValue()
        if ref in smt_work_by_ref:
            item = smt_work_by_ref[ref]
            pth = item['pad_numbers']
            pad_names = ', '.join(number or 'unnumbered' for number in pth)
            pad_info = f"{item['physical_joints_per_board']} PTH (pads {pad_names})"
            own = factory_notes.get(ref)
            if own and part == own['part'] and pth == own['pads']:
                pad_info += own.get('pads_suffix', '')
                note = own['note']
            else:
                note = 'Factory SMT placement. Owner solders the through-hole joints. Confirm assembly scope in the quote.'
        else:
            pads = list(fp.Pads())
            pth = sum(p.GetAttribute() == pcbnew.PAD_ATTRIB_PTH for p in pads)
            smd = sum(p.GetAttribute() == pcbnew.PAD_ATTRIB_SMD for p in pads)
            pad_info = ', '.join(f'{n} {kind}' for n, kind in ((pth, 'PTH'), (smd, 'SMD')) if n)
            note = notes['manual'].get(ref, '')
        hand.append(f"| {ref} | {'front' if fp.GetLayer() == pcbnew.F_Cu else 'back'} | {part} | {pad_info} | {note} |")
    if production_dnp:
        hand += ['', 'Leave unpopulated: ' + ', '.join(sorted(production_dnp)) + '.']
    for item in notes['when_unpopulated']:
        if set(item['refs']) <= set(production_dnp):
            hand.append(item['line'])
    for item in notes['when_factory']:
        if set(item['refs']) <= set(smt_refs):
            hand += ['', item['line']]
    return '\n'.join(hand) + '\n'


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--board", type=pathlib.Path, required=True)
    parser.add_argument("--production", type=pathlib.Path, required=True)
    parser.add_argument("--base-name", required=True)
    board_description.add_argument(parser)
    args = parser.parse_args()
    if pcbnew is None:
        raise RuntimeError("pcbnew is required to run kicad_finalize_jlc_outputs.py")
    description = board_description.load(args.board_description)

    board_path = args.board.resolve()
    production = args.production.resolve()
    bom_path = production / f"{args.base_name}_bom.csv"
    cpl_path = production / f"{args.base_name}_positions.csv"
    archive_path = production / f"{args.base_name}.zip"
    board = pcbnew.LoadBoard(str(board_path))

    footprints = {
        fp.GetReference(): fp
        for fp in board.GetFootprints()
        if not (fp.GetAttributes() & pcbnew.FP_EXCLUDE_FROM_BOM) and not fp.IsDNP()
    }
    manual_refs: set[str] = set()
    smt_refs: set[str] = set()
    for reference, footprint in footprints.items():
        # Placement classification is separate from any plated joints on an SMD part.
        fields = {str(f.GetName()): str(f.GetText()) for f in footprint.GetFields()}
        has_part = bool((fields.get("LCSC Part #") or fields.get("Supplier Part") or "").strip())
        if footprint.GetAttributes() & pcbnew.FP_SMD and has_part:
            smt_refs.add(reference)
        else:
            # SMD without an LCSC number is also hand-soldered.
            manual_refs.add(reference)

    source_bom, bom_fields = read_csv_with_fields(bom_path, BOM_FIELDS)
    source_cpl, cpl_fields = read_csv_with_fields(cpl_path, CPL_FIELDS)
    filtered_bom: list[dict[str, str]] = []
    source_bom_refs: set[str] = set()
    for row in source_bom:
        references = [part.strip() for part in row["Designator"].split(",")]
        source_bom_refs.update(references)
        kept = [reference for reference in references if reference in smt_refs]
        if not kept:
            continue
        output = dict(row)
        output["Designator"] = ", ".join(kept)
        output["Quantity"] = str(len(kept))
        filtered_bom.append(output)

    # One line per LCSC part number, all its designators together (JLCPCB's own
    # BOM format). With one line per designator, JLCPCB's matcher flags "Multiple
    # lines in the BOM have been matched to the same part" and leaves those lines
    # unticked, which means not placed (observed as 35 of 49 parts skipped). The
    # per-designator footprint names from the EasyEDA import (C0402__<hash>__C1)
    # are reduced to their package prefix for grouped lines.
    filtered_bom, grouped_labels = group_smt_bom_rows(filtered_bom)

    filtered_cpl = [row for row in source_cpl if row["Designator"] in smt_refs]
    bom_refs = {
        reference.strip()
        for row in filtered_bom
        for reference in row["Designator"].split(",")
    }
    cpl_refs = {row["Designator"] for row in filtered_cpl}
    missing_part = [row["Designator"] for row in filtered_bom if not row["LCSC Part #"].strip()]

    errors = validate_positions(source_cpl, footprints)
    copper = board_copper(board_path)
    extra_layers = board_extra_layers(board_path)
    archive_errors, paste_flashes = validate_fabrication_archive(archive_path, board, copper, extra_layers)
    errors.extend(archive_errors)
    if bom_refs != cpl_refs:
        errors.append(
            f"SMT BOM/CPL mismatch: bom_only={sorted(bom_refs-cpl_refs)} "
            f"cpl_only={sorted(cpl_refs-bom_refs)}"
        )
    if bom_refs != smt_refs:
        errors.append(
            f"SMT board/BOM mismatch: board_only={sorted(smt_refs-bom_refs)} "
            f"bom_only={sorted(bom_refs-smt_refs)}"
        )
    if source_bom_refs != smt_refs | manual_refs:
        errors.append(
            f"Full BOM mismatch: board_only={sorted((smt_refs|manual_refs)-source_bom_refs)} "
            f"bom_only={sorted(source_bom_refs-(smt_refs|manual_refs))}"
        )
    if missing_part:
        errors.append(f"SMT rows missing LCSC part numbers: {missing_part}")
    if errors:
        raise RuntimeError("\n".join(errors))

    smt_bom_path = production / f"{args.base_name}_SMT_bom.csv"
    smt_cpl_path = production / f"{args.base_name}_SMT_positions.csv"
    write_csv(smt_bom_path, filtered_bom, bom_fields)
    write_csv(smt_cpl_path, filtered_cpl, cpl_fields)

    with zipfile.ZipFile(archive_path) as archive:
        paste_regions = {
            side: sum(archive.read(name).count(b'G36*') for name in archive.namelist()
                      if pathlib.PurePosixPath(name).suffix.lower() == extension)
            for side, extension in [('top', '.gtp'), ('bottom', '.gbp')]
        }

    manifest = {
        "board": pathlib.Path(os.path.relpath(board_path, production)).as_posix(),
        'board_sha256': hashlib.sha256(board_path.read_bytes()).hexdigest().upper(),
        'validated_file_sha256': {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest().upper()
            for path in (bom_path, cpl_path, smt_bom_path, smt_cpl_path, archive_path)
        },
        "smt_component_count": len(smt_refs),
        "manual_through_hole_component_count": len(manual_refs),
        "manual_through_hole_references": sorted(manual_refs),
        "owner_manual_through_hole_on_smt": smt_manual_through_hole(footprints, smt_refs),
        'production_dnp': sorted(fp.GetReference() for fp in board.GetFootprints() if fp.IsDNP()),
        "smt_bom": smt_bom_path.name,
        "smt_positions": smt_cpl_path.name,
        "all_smt_parts_have_lcsc_numbers": True,
        "bom_cpl_reference_sets_match": True,
        "placement_coordinates_match_board_absolute_origin": True,
        "placement_rotation_convention": "JLCPCB CPL: front side KiCad angle, back side mirrored (180 - KiCad angle); positive counter-clockwise; matches JLCPCB placement viewer convention",
        "fabrication_archive": archive_path.name,
        "fabrication_archive_required_layers_present": True,
        "copper_layers": copper,
        "extra_fabrication_layers": extra_layers,
        "grouped_functional_labels": grouped_labels,
        "paste_flash_counts": {
            "top": paste_flashes[".gtp"],
            "bottom": paste_flashes[".gbp"],
        },
        'paste_region_counts': paste_regions,
        'paste_graphic_items': {
            side: {fp.GetReference(): sum(g.GetLayer() == layer for g in fp.GraphicalItems())
                   for fp in board.GetFootprints() if any(g.GetLayer() == layer for g in fp.GraphicalItems())}
            for side, layer in [('top', pcbnew.F_Paste), ('bottom', pcbnew.B_Paste)]
        },
        'paste_validation_scope': 'Pad flash counts checked here; packager also compares every complete exported fabrication layer, including all paste polygon regions.',
    }
    (production / "assembly-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    hand = hand_solder_document(args.base_name, footprints, manual_refs, smt_refs, manifest['production_dnp'],
                                manifest['owner_manual_through_hole_on_smt'], description['hand_solder_notes'])
    (production / 'HAND_SOLDER.md').write_text(hand, encoding='utf-8')
    print(json.dumps(manifest, sort_keys=True))


if __name__ == "__main__":
    main()
