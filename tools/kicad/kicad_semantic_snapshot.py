#!/usr/bin/env python3
"""Emit a deterministic, query-only semantic snapshot of the live PCB Editor."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from importlib.metadata import version as installed_version
from typing import Any

from kipy import KiCad
from kipy.board_types import ArcTrack, FootprintInstance, Pad, Track, Via
from kipy.proto.board.board_types_pb2 import BoardLayer, PadStackShape


SCHEMA_VERSION = 1
COPPER_LAYER_ENUM = r"^BL_(?:F_Cu|B_Cu|In[0-9]+_Cu)$"


def millimetres(nanometres: int) -> float:
    """Convert KiCad's integer nanometres to stable millimetres."""
    return round(nanometres / 1_000_000, 6)


def point_mm(point: Any) -> list[float]:
    return [millimetres(point.x), millimetres(point.y)]


def item_id(item: Any) -> str:
    return item.id.value


def layer_name(layer: int) -> str:
    enum_name = BoardLayer.Name(layer)
    if enum_name.startswith("BL_"):
        return ".".join(enum_name[3:].split("_"))
    return enum_name


def copper_layers(layers: Iterable[int]) -> list[str]:
    return sorted(
        layer_name(layer)
        for layer in layers
        if re.fullmatch(COPPER_LAYER_ENUM, BoardLayer.Name(layer))
    )


def net_row(net: Any) -> dict[str, Any]:
    return {"name": net.name}


def pad_copper_geometry(pad: Pad) -> list[dict[str, Any]]:
    return sorted(
        (
            {
                "layer": layer_name(layer.layer),
                "offset_mm": point_mm(layer.offset),
                "shape": PadStackShape.Name(layer.shape),
                "size_mm": point_mm(layer.size),
            }
            for layer in pad.padstack.copper_layers
        ),
        key=lambda row: row["layer"],
    )


def footprint_row(footprint: FootprintInstance) -> dict[str, Any]:
    return {
        "id": item_id(footprint),
        "layer": layer_name(footprint.layer),
        "orientation_deg": round(footprint.orientation.degrees, 6),
        "position_mm": point_mm(footprint.position),
        "reference": footprint.reference_field.text.value,
        "value": footprint.value_field.text.value,
    }


def pad_row(pad: Pad, owner: dict[str, str] | None) -> dict[str, Any]:
    geometry = pad_copper_geometry(pad)
    row: dict[str, Any] = {
        "copper_geometry": geometry,
        "id": item_id(pad),
        "layers": [entry["layer"] for entry in geometry],
        "net": net_row(pad.net),
        "number": pad.number,
        "pad_type": int(pad.pad_type),
        "position_mm": point_mm(pad.position),
    }
    if owner is not None:
        row["footprint_id"] = owner["id"]
        row["reference"] = owner["reference"]
    return row


def track_row(track: Track | ArcTrack) -> dict[str, Any]:
    row: dict[str, Any] = {
        "end_mm": point_mm(track.end),
        "id": item_id(track),
        "kind": "arc" if isinstance(track, ArcTrack) else "segment",
        "layer": layer_name(track.layer),
        "net": net_row(track.net),
        "start_mm": point_mm(track.start),
        "width_mm": millimetres(track.width),
    }
    if isinstance(track, ArcTrack):
        row["mid_mm"] = point_mm(track.mid)
    return row


def via_row(via: Via) -> dict[str, Any]:
    return {
        "diameter_mm": millimetres(via.diameter),
        "drill_diameter_mm": millimetres(via.drill_diameter),
        "id": item_id(via),
        "net": net_row(via.net),
        "position_mm": point_mm(via.position),
        "via_type": int(via.type),
    }


def selection_row(item: Any, details_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    identifier = item_id(item)
    row: dict[str, Any] = {"id": identifier, "kind": type(item).__name__}
    details = details_by_id.get(identifier)
    if details is not None:
        for key in ("reference", "number", "net", "layer", "layers", "position_mm"):
            if key in details:
                row[key] = details[key]
    return row


def sorted_rows(rows: Sequence[dict[str, Any]], *keys: str) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: tuple(str(row.get(key, "")) for key in keys))


def main() -> None:
    kicad = KiCad(timeout_ms=3_000)
    board = kicad.get_board()

    footprints = list(board.get_footprints())
    pads = list(board.get_pads())
    nets = list(board.get_nets())
    tracks = list(board.get_tracks())
    vias = list(board.get_vias())

    footprint_rows = [footprint_row(footprint) for footprint in footprints]
    owners: dict[str, dict[str, str]] = {}
    for footprint, row in zip(footprints, footprint_rows):   # same length by construction; no strict=: KiCad's Mac Python is 3.9
        for pad in footprint.definition.pads:
            owners[item_id(pad)] = {"id": row["id"], "reference": row["reference"]}

    missing_owners = sorted({item_id(pad) for pad in pads} - set(owners))
    if missing_owners:
        raise RuntimeError(
            "Live API did not expose a footprint owner for board pad IDs: "
            + ", ".join(missing_owners)
        )

    pad_rows = [pad_row(pad, owners.get(item_id(pad))) for pad in pads]
    track_rows = [track_row(track) for track in tracks]
    via_rows = [via_row(via) for via in vias]
    net_rows = [net_row(net) for net in nets]

    details_by_id = {
        row["id"]: row
        for rows in (footprint_rows, pad_rows, track_rows, via_rows)
        for row in rows
    }
    selection = [selection_row(item, details_by_id) for item in board.get_selection()]

    snapshot = {
        "active_layer": layer_name(board.get_active_layer()),
        "active_tool": None,
        "api_versions": {
            "kicad": str(kicad.get_version()),
            "kicad_api": str(kicad.get_api_version()),
            "kicad_python": installed_version("kicad-python"),
        },
        "copper_layers": {
            "enabled_and_visible": sorted(
                set(copper_layers(board.get_enabled_layers()))
                & set(copper_layers(board.get_visible_layers()))
            )
        },
        "counts": {
            "footprints": len(footprints),
            "nets": len(nets),
            "pads": len(pads),
            "track_arcs": sum(isinstance(track, ArcTrack) for track in tracks),
            "track_segments": sum(isinstance(track, Track) for track in tracks),
            "vias": len(vias),
        },
        "document": {
            "board_filename": board.document.board_filename,
            "project_path": str(board.document.project.path),
        },
        "footprints": sorted_rows(footprint_rows, "reference", "id"),
        "hardware_verdict": "NONE",
        "nets": sorted_rows(net_rows, "name"),
        "pads": sorted_rows(pad_rows, "reference", "number", "id"),
        "query_only": True,
        "router_mode": None,
        "schema_version": SCHEMA_VERSION,
        "selection": sorted_rows(selection, "kind", "id"),
        "tracks": sorted_rows(track_rows, "net", "layer", "id"),
        "vias": sorted_rows(via_rows, "net", "id"),
        "viewport": None,
    }
    print(json.dumps(snapshot, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
