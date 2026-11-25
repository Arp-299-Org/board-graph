"""Report through-hole pads that join a copper zone other than through thermal spokes.

The fabricated V2 carrier could barely be soldered by hand: a through-hole pad
tied solid to a plane sinks the iron's heat into it.  This offline report looks
at every plated through-hole pad on every copper layer where a filled zone of
its net reaches it, and gives two things for each: the zone connection the
settings ask for (the pad's own override, else its footprint's, else the
zone's), and the share of copper the zone's SAVED fill puts in a band
BAND_INNER_MM to BAND_OUTER_MM outside the pad's copper.  Spokes leave that
band mostly empty; a solid joint fills it.  A pad is a finding when the fill
touches it and either the setting is solid or the measured share is above
THRESHOLD, so a setting that says thermal never passes a fill that is solid.

Surface-mount pads (the factory reflows them), pads of nets without a zone and
net-tie footprints are counted, not judged.  Vias are not pads.  Custom DRC
rules can also set a zone connection; the measurement sees their effect.

Usage (offline, read-only):
  .\\scripts\\kicad_run.ps1 -Mode Offline -Project board `
      -Script scripts\\kicad_report_thermal_relief.py -ScriptArgs @('--board', '<path>.kicad_pcb')
"""

from __future__ import annotations

import argparse
import json

import pcbnew

BAND_INNER_MM, BAND_OUTER_MM = 0.15, 0.35
TOUCH_MM = 0.02  # the fill counts as touching the pad when it comes this close to its copper
THRESHOLD = 0.40
THRESHOLD_SOURCE = (
    "Empirical separation between thermal-spoke fills (around 0.14 to 0.23 mm perimeter fraction) and "
    "solid plane connections (above 0.60 mm); 0.40 sits in the gap")

CONNECTION_NAMES = {
    pcbnew.ZONE_CONNECTION_FULL: "solid",
    pcbnew.ZONE_CONNECTION_THERMAL: "thermal",
    pcbnew.ZONE_CONNECTION_THT_THERMAL: "thermal (through-hole only)",
    pcbnew.ZONE_CONNECTION_NONE: "none",
}


def mm(value_mm: float) -> int:
    return pcbnew.FromMM(value_mm)


def effective_connection(pad, footprint, zone) -> tuple[int, str]:
    """The connection the settings ask for and whose setting it is."""
    if pad.GetLocalZoneConnection() != pcbnew.ZONE_CONNECTION_INHERITED:
        return pad.GetLocalZoneConnection(), "pad"
    if footprint.GetLocalZoneConnection() != pcbnew.ZONE_CONNECTION_INHERITED:
        return footprint.GetLocalZoneConnection(), "footprint"
    return zone.GetPadConnection(), "zone"


def copper_shape(pad, layer: int, max_error: int):
    """The pad's copper on the layer; the hole's outline where the pad has no copper of its own there."""
    shape = pcbnew.SHAPE_POLY_SET()
    if pad.FlashLayer(layer):
        pad.TransformShapeToPolygon(shape, layer, 0, max_error, pcbnew.ERROR_INSIDE)
    else:
        pad.TransformHoleToPolygon(shape, 0, max_error, pcbnew.ERROR_INSIDE)
    return shape


def grown(shape, amount_mm: float, max_error: int):
    out = pcbnew.SHAPE_POLY_SET(shape)
    out.Inflate(mm(amount_mm), pcbnew.CORNER_STRATEGY_ROUND_ALL_CORNERS, max_error)
    return out


def area_inside(region, fill) -> float:
    both = pcbnew.SHAPE_POLY_SET(region)
    both.BooleanIntersection(fill)
    return both.Area()


def measure(pad, layer: int, fill, max_error: int) -> tuple[bool, bool, float]:
    """Whether the fill reaches the band's outer edge, whether it touches the pad, and the band's copper share."""
    shape = copper_shape(pad, layer, max_error)
    outer = grown(shape, BAND_OUTER_MM, max_error)
    if area_inside(outer, fill) <= 0:
        return False, False, 0.0
    band = pcbnew.SHAPE_POLY_SET(outer)
    band.BooleanSubtract(grown(shape, BAND_INNER_MM, max_error))
    touches = area_inside(grown(shape, TOUCH_MM, max_error), fill) > 0
    return True, touches, area_inside(band, fill) / band.Area()


def layer_name(board, layer: int) -> str:
    """KiCad's own name, with the board's name for the layer when it has one: In1.Cu (GND_REF_TOP)."""
    standard, user = pcbnew.BOARD.GetStandardLayerName(layer), board.GetLayerName(layer)
    return standard if user == standard else f"{standard} ({user})"


def is_plane(zone) -> bool:
    teardrop = getattr(zone, "IsTeardropArea", lambda: False)()
    return not zone.GetIsRuleArea() and not teardrop and zone.IsFilled()


def thermal_report(board) -> dict:
    max_error = board.GetDesignSettings().m_MaxError
    zones_by_net: dict[int, list] = {}
    for zone in board.Zones():
        if is_plane(zone) and zone.GetNetCode() > 0:
            zones_by_net.setdefault(zone.GetNetCode(), []).append(zone)
    rows, findings = [], {}  # findings by pad: one pad joined solid on two planes is one finding
    counts = {"through_hole_pads_checked": 0, "through_hole_pads_net_without_zone": 0,
              "surface_mount_pads": 0, "other_pads": 0, "net_tie_pads": 0}
    for fp in board.GetFootprints():
        if fp.IsNetTie():
            counts["net_tie_pads"] += len(fp.Pads())
            continue
        for pad in fp.Pads():
            attr = pad.GetAttribute()
            if attr == pcbnew.PAD_ATTRIB_SMD:
                counts["surface_mount_pads"] += 1
                continue
            if attr != pcbnew.PAD_ATTRIB_PTH:
                counts["other_pads"] += 1  # unplated holes and edge connectors
                continue
            zones = zones_by_net.get(pad.GetNetCode(), []) if pad.GetNetCode() > 0 else []
            if not zones:
                counts["through_hole_pads_net_without_zone"] += 1
                continue
            counts["through_hole_pads_checked"] += 1
            pos = pad.GetPosition()
            for zone in zones:
                for layer in zone.GetLayerSet().Seq():
                    if not pcbnew.IsCopperLayer(layer) or not pad.IsOnLayer(layer) \
                            or not zone.HasFilledPolysForLayer(layer):
                        continue
                    reaches, touches, share = measure(pad, layer, zone.GetFilledPolysList(layer), max_error)
                    if not reaches:
                        continue
                    connection, source = effective_connection(pad, fp, zone)
                    row = {
                        "ref": fp.GetReference(), "pad": pad.GetNumber(), "net": pad.GetNetname(),
                        "x_mm": round(pcbnew.ToMM(pos.x), 3), "y_mm": round(pcbnew.ToMM(pos.y), 3),
                        "layer": layer_name(board, layer), "zone": zone.GetZoneName() or zone.m_Uuid.AsString(),
                        "connection": CONNECTION_NAMES.get(connection, str(connection)),
                        "connection_from": source, "fill_touches_pad": touches, "band_share": round(share, 3),
                    }
                    reasons = []
                    if touches and connection == pcbnew.ZONE_CONNECTION_FULL:
                        reasons.append("the setting is solid")
                    if touches and share > THRESHOLD:
                        reasons.append(f"the saved fill covers {share:.0%} of the band, above {THRESHOLD:.0%}")
                    row["finding"] = bool(reasons)
                    rows.append(row)
                    if reasons:
                        key = (row["ref"], row["pad"], row["x_mm"], row["y_mm"])
                        found = findings.setdefault(key, {k: row[k] for k in ("ref", "pad", "net", "x_mm", "y_mm")}
                                                    | {"layers": []})
                        found["layers"].append({"layer": row["layer"], "connection": row["connection"],
                                                "band_share": row["band_share"], "reasons": reasons})
    return {
        "board": board.GetFileName(),
        "band_mm": [BAND_INNER_MM, BAND_OUTER_MM],
        "threshold": THRESHOLD,
        "threshold_source": THRESHOLD_SOURCE,
        **counts,
        "rows": rows,
        "findings": list(findings.values()),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--board", required=True)
    ap.add_argument("--json")
    a = ap.parse_args()
    rep = thermal_report(pcbnew.LoadBoard(a.board))
    print(f"board: {rep['board']}")
    print(f"through-hole pads on a zone's net: {rep['through_hole_pads_checked']}  "
          f"without a zone: {rep['through_hole_pads_net_without_zone']}  surface-mount (not judged): "
          f"{rep['surface_mount_pads']}  net tie: {rep['net_tie_pads']}  other: {rep['other_pads']}")
    print(f"copper share of the band {BAND_INNER_MM} to {BAND_OUTER_MM} mm outside each pad, saved fill; "
          f"finding above {THRESHOLD:.0%} ({THRESHOLD_SOURCE})")
    for r in rep["rows"]:
        print(f"  {r['ref']}.{r['pad']} ({r['x_mm']}, {r['y_mm']}) {r['net']} {r['layer']}: "
              f"{r['connection']} from {r['connection_from']}, touches={r['fill_touches_pad']}, "
              f"share {r['band_share']:.3f}{'  FINDING' if r['finding'] else ''}")
    print(f"through-hole pads joined to a zone other than by spokes: {len(rep['findings'])}")
    for f in rep["findings"]:
        for at in f["layers"]:
            print(f"  {f['ref']}.{f['pad']} ({f['x_mm']}, {f['y_mm']}) {at['layer']}: {'; '.join(at['reasons'])}")
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(rep, fh, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
