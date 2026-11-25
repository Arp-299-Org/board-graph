#!/usr/bin/env python3
"""Generate JLC-compatible full BOM/CPL source files from the repaired PCB.

These name every fitted part, the hand-soldered ones too, and are never uploaded as they are:
kicad_finalize_jlc_outputs.py writes the factory's SMT-only files from them. Nothing is written while a
part's LCSC Part # and Supplier Part disagree (board_graph_actions.lcsc_field_findings).

Placement coordinates intentionally use the board's absolute coordinate system.
The production Gerbers and Excellon files are exported with an absolute origin,
so using KiCad's auxiliary origin here would offset every assembly placement.

Rotation: front-side parts carry KiCad's angle. Back-side parts carry the angle
mirrored, (180 - KiCad angle), because that is how JLCPCB's placement viewer
reads the back side (preserving 90 and -90 while flipping 0 and 180 by half a turn).
"""

from __future__ import annotations

import argparse
import csv
import math
import pathlib

import pcbnew

from board_graph_actions import lcsc_field_findings


def field_map(footprint: pcbnew.FOOTPRINT) -> dict[str, str]:
    return {str(field.GetName()): str(field.GetText()) for field in footprint.GetFields()}


def jlc_rotation(kicad_angle: float, top: bool) -> float:
    """CPL angle JLCPCB needs: KiCad's on the front, mirrored (180 - a) on the back.

    Normalised to (-180, 180]. Kept identical to the copy in
    kicad_finalize_jlc_outputs.py, which validates against it.
    """
    angle = kicad_angle if top else 180.0 - kicad_angle
    angle = (angle + 180.0) % 360.0 - 180.0
    if math.isclose(angle, -180.0, abs_tol=1e-9):
        angle = 180.0
    return round(angle, 4) + 0.0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--board", type=pathlib.Path, required=True)
    parser.add_argument("--production", type=pathlib.Path, required=True)
    parser.add_argument("--base-name", required=True)
    args = parser.parse_args()

    board = pcbnew.LoadBoard(str(args.board.resolve()))
    production = args.production.resolve()
    footprints = []
    for footprint in board.GetFootprints():
        if footprint.GetAttributes() & pcbnew.FP_EXCLUDE_FROM_BOM:
            continue
        if footprint.IsDNP():
            continue
        footprints.append(footprint)
    footprints.sort(key=lambda footprint: footprint.GetReference())

    # The BOM below takes LCSC Part # first, so it must name the part Supplier Part (the schematic's) names.
    disagreements = lcsc_field_findings(
        (footprint.GetReference(), bool(footprint.GetAttributes() & pcbnew.FP_SMD),
         field_map(footprint).get("LCSC Part #"), field_map(footprint).get("Supplier Part"))
        for footprint in footprints)
    if disagreements:
        names = ", ".join(sorted({line.split(":", 1)[0] for line in disagreements}))
        raise SystemExit(f"Refusing to write the order files: LCSC Part # and Supplier Part disagree on {names}.\n"
                         + "\n".join(disagreements))
    production.mkdir(parents=True, exist_ok=True)

    bom_path = production / f"{args.base_name}_bom.csv"
    with bom_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["Designator", "Footprint", "Quantity", "Value", "LCSC Part #"],
        )
        writer.writeheader()
        for footprint in footprints:
            fields = field_map(footprint)
            writer.writerow(
                {
                    "Designator": footprint.GetReference(),
                    "Footprint": str(footprint.GetFPID().GetLibItemName()),
                    "Quantity": "1",
                    "Value": footprint.GetValue(),
                    "LCSC Part #": fields.get("LCSC Part #", fields.get("Supplier Part", "")),
                }
            )

    positions_path = production / f"{args.base_name}_positions.csv"
    with positions_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["Designator", "Mid X", "Mid Y", "Rotation", "Layer"],
        )
        writer.writeheader()
        for footprint in footprints:
            position = footprint.GetPosition()
            top = footprint.GetLayer() == pcbnew.F_Cu
            angle = footprint.GetOrientationDegrees()
            writer.writerow(
                {
                    "Designator": footprint.GetReference(),
                    "Mid X": round(position.x / 1_000_000.0, 6),
                    "Mid Y": round(-position.y / 1_000_000.0, 6),
                    "Rotation": jlc_rotation(angle, top),
                    "Layer": "top" if top else "bottom",
                }
            )

    designators = production / f"{args.base_name}_designators.csv"
    designators.write_text(
        "".join(f"{footprint.GetReference()}:1\n" for footprint in footprints),
        encoding="utf-8",
    )
    print(
        f"components={len(footprints)} bom={bom_path.name} "
        f"positions={positions_path.name} designators={designators.name} "
        "coordinate_origin=absolute"
    )


if __name__ == "__main__":
    main()
