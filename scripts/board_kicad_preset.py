#!/usr/bin/env python3
"""Fab-ready KiCad 10 project, DRC rules, and minimal schematic presets for `board-graph init --fab`."""
from __future__ import annotations

import json
import pathlib

FAB_PRESETS = ("jlcpcb-2layer", "jlcpcb-4layer")


def kicad_pro_data(board_name: str, fab: str = "jlcpcb-2layer") -> dict:
    """Return a clean KiCad 10 `.kicad_pro` dict with JLCPCB design rule minimums."""
    if fab not in FAB_PRESETS:
        raise ValueError(f"Unsupported fab preset {fab!r}; choose from {', '.join(FAB_PRESETS)}")
    copper_layers = 4 if fab == "jlcpcb-4layer" else 2
    return {
        "board": {
            "3dviewsettings": {},
            "design_settings": {
                "defaults": {
                    "apply_defaults_to_fp_fields": False,
                    "apply_defaults_to_fp_shapes": False,
                    "apply_defaults_to_fp_text": False,
                    "board_outline_line_width": 0.1,
                    "copper_line_width": 0.25,
                    "copper_text_italic": False,
                    "copper_text_size_h": 1.5,
                    "copper_text_size_v": 1.5,
                    "copper_text_thickness": 0.3,
                    "silk_line_width": 0.15,
                    "silk_text_size_h": 1.0,
                    "silk_text_size_v": 1.0,
                    "silk_text_thickness": 0.15,
                },
                "rules": {
                    "min_clearance": 0.15,
                    "min_copper_edge_clearance": 0.3,
                    "min_hole_clearance": 0.25,
                    "min_hole_to_hole": 0.5,
                    "min_microvia_diameter": 0.2,
                    "min_microvia_drill": 0.1,
                    "min_silk_clearance": 0.15,
                    "min_text_height": 1.0,
                    "min_text_thickness": 0.15,
                    "min_through_hole_diameter": 0.3,
                    "min_track_width": 0.15,
                    "min_via_annular_width": 0.13,
                    "min_via_diameter": 0.45,
                    "solder_mask_to_copper_clearance": 0.0,
                },
                "stackup": {"copper_layer_count": copper_layers},
            },
        },
        "meta": {"filename": f"{board_name}.kicad_pro", "version": 3},
        "net_settings": {
            "classes": [
                {
                    "clearance": 0.2,
                    "name": "Default",
                    "track_width": 0.25,
                    "via_diameter": 0.6,
                    "via_drill": 0.3,
                }
            ]
        },
        "sheets": [],
    }


def kicad_dru_text(fab: str = "jlcpcb-2layer") -> str:
    """Return a KiCad 10 `.kicad_dru` file encoding JLCPCB silkscreen and board-edge checks."""
    if fab not in FAB_PRESETS:
        raise ValueError(f"Unsupported fab preset {fab!r}; choose from {', '.join(FAB_PRESETS)}")
    return (
        "(version 1)\n"
        f"# {fab} baseline fabrication rules\n"
        '(rule "JLCPCB silkscreen legibility"\n'
        '  (constraint text_height (min 1.0mm))\n'
        '  (constraint text_thickness (min 0.15mm))\n'
        "  (condition \"A.Layer == 'F.SilkS' || A.Layer == 'B.SilkS'\"))\n"
        '(rule "JLCPCB copper edge clearance"\n'
        '  (constraint edge_clearance (min 0.3mm)))\n'
    )


def empty_schematic_text() -> str:
    """Return a minimal valid KiCad 10 `.kicad_sch` skeleton."""
    return (
        "(kicad_sch\n"
        "\t(version 20250114)\n"
        '\t(generator "eeschema")\n'
        '\t(generator_version "10.0")\n'
        '\t(uuid "00000000-0000-4000-8000-000000000001")\n'
        '\t(paper "A4")\n'
        "\t(lib_symbols)\n"
        "\t(sheet_instances\n"
        '\t\t(path "/"\n'
        '\t\t\t(page "1")\n'
        "\t\t)\n"
        "\t)\n"
        ")\n"
    )


def write_kicad_presets(target: pathlib.Path, board_name: str, fab: str = "jlcpcb-2layer") -> list[str]:
    """Write absent `.kicad_pro`, `.kicad_dru`, and `.kicad_sch` preset files under `target/board`."""
    board_dir = target / "board"
    board_dir.mkdir(parents=True, exist_ok=True)
    candidates = {
        f"board/{board_name}.kicad_pro": json.dumps(kicad_pro_data(board_name, fab), indent=2) + "\n",
        f"board/{board_name}.kicad_dru": kicad_dru_text(fab),
        f"board/{board_name}.kicad_sch": empty_schematic_text(),
    }
    written: list[str] = []
    for rel, content in candidates.items():
        dest = target / rel
        if not dest.exists():
            dest.write_text(content, encoding="utf-8")
            written.append(rel)
    return written
