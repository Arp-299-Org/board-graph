"""Report pads whose copper is not exposed through the solder mask.

The fabricated V2 carrier shipped with hand-soldered through-hole lands under
solder mask.  This offline
report lists every pad that has copper on a layer without a matching mask
aperture, plus the board-level mask expansion, so that a board's package can be
checked against the same failure before any order.

Usage (offline, read-only):
  .\\scripts\\kicad_run.ps1 -Mode Offline -Project <board folder> `
      -Script scripts\\kicad_report_pad_mask.py -ScriptArgs @('--board', '<path>.kicad_pcb')
"""

from __future__ import annotations

import argparse
import json

import pcbnew

F_CU, B_CU = pcbnew.F_Cu, pcbnew.B_Cu
F_MASK, B_MASK = pcbnew.F_Mask, pcbnew.B_Mask


def pad_report(board: pcbnew.BOARD) -> dict:
    ds = board.GetDesignSettings()
    findings, counted, net_tie_pads = [], 0, 0
    for fp in board.GetFootprints():
        if fp.IsNetTie():
            net_tie_pads += len(fp.Pads())  # a net tie (NT1) is copper joining two nets under mask by design, not a hand-solder land
            continue
        for pad in fp.Pads():
            counted += 1
            layers = pad.GetLayerSet()
            has_cu_f, has_cu_b = layers.Contains(F_CU), layers.Contains(B_CU)
            has_mask_f, has_mask_b = layers.Contains(F_MASK), layers.Contains(B_MASK)
            attr = pad.GetAttribute()
            kind = {pcbnew.PAD_ATTRIB_PTH: "PTH", pcbnew.PAD_ATTRIB_SMD: "SMD",
                    pcbnew.PAD_ATTRIB_NPTH: "NPTH", pcbnew.PAD_ATTRIB_CONN: "CONN"}.get(attr, str(attr))
            if kind == "NPTH":
                continue
            missing = []
            if has_cu_f and not has_mask_f:
                missing.append("F.Mask")
            if has_cu_b and not has_mask_b:
                missing.append("B.Mask")
            # A negative local expansion larger than the pad closes the aperture.
            local = pcbnew.ToMM(pad.GetLocalSolderMaskMargin()) if pad.GetLocalSolderMaskMargin() is not None else None
            fp_local = pcbnew.ToMM(fp.GetLocalSolderMaskMargin()) if fp.GetLocalSolderMaskMargin() is not None else None
            eff = local if local is not None else (fp_local if fp_local is not None else pcbnew.ToMM(ds.m_SolderMaskExpansion))
            sz = pad.GetSize()
            closed = eff < 0 and (2 * -eff >= pcbnew.ToMM(min(sz.x, sz.y)))
            if missing or closed:
                findings.append({
                    "ref": fp.GetReference(), "pad": pad.GetNumber(), "kind": kind,
                    "net": pad.GetNetname(), "missing_mask_layers": missing,
                    "effective_mask_expansion_mm": round(eff, 3), "aperture_closed": closed,
                })
    return {
        "board": board.GetFileName(),
        "pads_checked": counted,
        "pads_skipped": {"net_tie": net_tie_pads},  # with pads_checked, every pad the board has
        "board_mask_expansion_mm": round(pcbnew.ToMM(ds.m_SolderMaskExpansion), 3),
        "board_mask_min_web_mm": round(pcbnew.ToMM(ds.m_SolderMaskMinWidth), 3),
        "tent_vias_front": bool(getattr(ds, "m_TentViasFront", True)),
        "tent_vias_back": bool(getattr(ds, "m_TentViasBack", True)),
        "findings": findings,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--board", required=True)
    ap.add_argument("--json")
    a = ap.parse_args()
    rep = pad_report(pcbnew.LoadBoard(a.board))
    print(f"board: {rep['board']}")
    print(f"pads checked: {rep['pads_checked']}  mask expansion: {rep['board_mask_expansion_mm']} mm  "
          f"min web: {rep['board_mask_min_web_mm']} mm  tent vias F/B: {rep['tent_vias_front']}/{rep['tent_vias_back']}")
    print(f"pads without a mask aperture over their copper: {len(rep['findings'])}")
    for f in rep["findings"]:
        print(f"  {f['ref']}.{f['pad']} {f['kind']} {f['net']} missing={f['missing_mask_layers']} "
              f"exp={f['effective_mask_expansion_mm']} closed={f['aperture_closed']}")
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(rep, fh, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
