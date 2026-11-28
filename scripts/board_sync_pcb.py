#!/usr/bin/env python3
"""Sync a KiCad schematic netlist cleanly into a saved PCB offline via kicad-cli and pcbnew."""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import board_bom_export  # noqa: E402
import board_description  # noqa: E402
import board_sync_models  # noqa: E402
import kicad_sexp  # noqa: E402
import tool_paths  # noqa: E402
from no_window import KWARGS as NO_WINDOW  # noqa: E402

if __name__ == "__main__":
    tool_paths.take_argument()

TOOL_ROOT = tool_paths.TOOL_ROOT
ROOT = tool_paths.project_root()

SYNC_FIELDS = (
    "Value",
    "Manufacturer",
    "Manufacturer Part",
    "MPN",
    "Supplier",
    "Supplier Part",
    "LCSC Part #",
    "Datasheet",
)
STOCK_FP_CANDIDATES = (
    "/usr/share/kicad/footprints",
    "/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints",
    "C:/Program Files/KiCad/10.0/share/kicad/footprints",
)


def _sch_symbol_overlay(sch_text: str) -> dict[str, dict]:
    tree = kicad_sexp.parse(sch_text)
    overlay: dict[str, dict] = {}
    for sym in kicad_sexp.children(tree, "symbol"):
        ref = str(kicad_sexp.prop(sym, "Reference") or "").strip()
        if not ref or ref.startswith("#"):
            continue
        fields = {
            name: str(val)
            for name in SYNC_FIELDS
            for val in (kicad_sexp.prop(sym, name),)
            if val is not None and str(val).strip()
        }
        overlay[ref] = {
            "fields": fields,
            "dnp": kicad_sexp.child(sym, "dnp") == ["dnp", "yes"],
            "exclude_from_bom": kicad_sexp.child(sym, "in_bom") == ["in_bom", "no"],
        }
    return overlay


def _comp_fields(comp: list) -> dict[str, str]:
    fields: dict[str, str] = {}
    fields_node = kicad_sexp.child(comp, "fields")
    if fields_node:
        for item in kicad_sexp.children(fields_node, "field"):
            name_node = kicad_sexp.child(item, "name")
            name = str(name_node[1]) if name_node and len(name_node) > 1 else ""
            val = str(item[-1]) if len(item) >= 3 and isinstance(item[-1], str) else ""
            if name and val:
                fields[name] = val
    for pnode in kicad_sexp.children(comp, "property"):
        name_node = kicad_sexp.child(pnode, "name")
        val_node = kicad_sexp.child(pnode, "value")
        name = str(name_node[1]) if name_node and len(name_node) > 1 else ""
        val = str(val_node[1]) if val_node and len(val_node) > 1 else ""
        if name and val and name not in ("Sheetname", "Sheetfile", "ki_keywords", "ki_fp_filters"):
            fields.setdefault(name, val)
    ds_node = kicad_sexp.child(comp, "datasheet")
    if ds_node and len(ds_node) > 1 and str(ds_node[1]).strip():
        fields.setdefault("Datasheet", str(ds_node[1]).strip())
    return fields


def parse_netlist(netlist_text: str, sch_text: str | None = None) -> dict:
    """Parse KiCad kicadsexpr netlist (with optional schematic property overlay) into components and nets."""
    tree = kicad_sexp.parse(netlist_text)
    overlay = _sch_symbol_overlay(sch_text) if sch_text else {}
    components: list[dict] = []
    comps_node = kicad_sexp.child(tree, "components")
    for comp in kicad_sexp.children(comps_node or [], "comp"):
        ref_node = kicad_sexp.child(comp, "ref")
        val_node = kicad_sexp.child(comp, "value")
        fp_node = kicad_sexp.child(comp, "footprint")
        ref = str(ref_node[1]).strip() if ref_node and len(ref_node) > 1 else ""
        value = str(val_node[1]).strip() if val_node and len(val_node) > 1 else ""
        fp_id = str(fp_node[1]).strip() if fp_node and len(fp_node) > 1 else ""
        if not ref:
            continue
        lib_nick, fp_name = fp_id.split(":", 1) if ":" in fp_id else ("", fp_id)
        fields = _comp_fields(comp)
        ov = overlay.get(ref, {})
        for k, v in ov.get("fields", {}).items():
            fields[k] = v
        if fields.get("Value"):
            value = fields["Value"]
        elif value:
            fields["Value"] = value
        dnp = bool(ov.get("dnp") or kicad_sexp.child(comp, "dnp") is not None)
        ex_bom = bool(ov.get("exclude_from_bom") or kicad_sexp.child(comp, "exclude_from_bom") is not None)
        if not dnp and not ex_bom and fields.get("Supplier Part") and not fields.get("LCSC Part #"):
            fields["LCSC Part #"] = fields["Supplier Part"]
        components.append({
            "ref": ref,
            "value": value,
            "footprint": fp_id,
            "lib_nick": lib_nick,
            "fp_name": fp_name,
            "fields": fields,
            "dnp": dnp,
            "exclude_from_bom": ex_bom,
        })

    nets: dict[str, list[list[str]]] = {}
    nets_node = kicad_sexp.child(tree, "nets")
    for net in kicad_sexp.children(nets_node or [], "net"):
        name_node = kicad_sexp.child(net, "name")
        net_name = str(name_node[1]) if name_node and len(name_node) > 1 else ""
        if not net_name:
            continue
        nodes: list[list[str]] = []
        for nd in kicad_sexp.children(net, "node"):
            n_ref = kicad_sexp.child(nd, "ref")
            n_pin = kicad_sexp.child(nd, "pin")
            if n_ref and len(n_ref) > 1 and n_pin and len(n_pin) > 1:
                nodes.append([str(n_ref[1]), str(n_pin[1])])
        nets[net_name] = nodes
    return {"components": components, "nets": nets}


def parse_fp_lib_table(
    table_path: pathlib.Path,
    board_dir: pathlib.Path,
    default_lib: tuple[str, pathlib.Path] | None = None,
) -> dict[str, pathlib.Path]:
    """Resolve footprint library nicknames to directories from `fp-lib-table`."""
    stock_root = next((p for p in STOCK_FP_CANDIDATES if pathlib.Path(p).is_dir()), STOCK_FP_CANDIDATES[0])
    libs: dict[str, pathlib.Path] = {}
    if table_path.is_file():
        tree = kicad_sexp.parse(table_path.read_text(encoding="utf-8"))
        for entry in kicad_sexp.children(tree, "lib"):
            n_node = kicad_sexp.child(entry, "name")
            u_node = kicad_sexp.child(entry, "uri")
            if not n_node or len(n_node) < 2 or not u_node or len(u_node) < 2:
                continue
            raw_uri = str(u_node[1]).replace("${KIPRJMOD}", str(board_dir))
            for var in ("${KICAD10_FOOTPRINT_DIR}", "${KICAD9_FOOTPRINT_DIR}"):
                raw_uri = raw_uri.replace(var, stock_root)
            libs[str(n_node[1])] = pathlib.Path(os.path.expandvars(raw_uri)).expanduser().resolve()
    if default_lib and default_lib[0] not in libs:
        libs[default_lib[0]] = default_lib[1].resolve()
    return libs


def _project_copper_layers(pcb_path: pathlib.Path) -> int | None:
    """Return stackup copper_layer_count from sibling `.kicad_pro` when >= 2, else None."""
    pro_path = pcb_path.with_suffix(".kicad_pro")
    if not pro_path.is_file():
        return None
    try:
        data = json.loads(pro_path.read_text(encoding="utf-8"))
        count = int(data["board"]["design_settings"]["stackup"]["copper_layer_count"])
    except (OSError, ValueError, TypeError, KeyError):
        return None
    return count if count >= 2 else None


PCB_SYNC_WORKER = """
import json, pathlib, sys, pcbnew
spec = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))
pcb_file = pathlib.Path(spec['pcb_path'])
if pcb_file.is_file():
    board = pcbnew.LoadBoard(str(pcb_file))
else:
    board = pcbnew.CreateEmptyBoard()
    if spec.get('copper_layers') and hasattr(board, 'SetCopperLayerCount'):
        board.SetCopperLayerCount(int(spec['copper_layers']))
net_map = {}
for net_name in spec['nets']:
    net = board.FindNet(net_name)
    if net is None:
        net = pcbnew.NETINFO_ITEM(board, net_name)
        board.Add(net)
    net_map[net_name] = net
pad_net = {(ref, pin): net_name for net_name, pairs in spec['nets'].items() for ref, pin in pairs}
lib_dirs = spec['lib_dirs']
added = updated = 0
for idx, comp in enumerate(spec['components']):
    ref, nick, fp_name = comp['ref'], comp['lib_nick'], comp['fp_name']
    lib_dir = lib_dirs.get(nick) or (next(iter(lib_dirs.values()), '') if not nick else '')
    existing = board.FindFootprintByReference(ref)
    if existing is None or spec['replace_footprints'] or existing.GetFPID().GetLibItemName() != fp_name:
        if not lib_dir:
            raise RuntimeError(f"{ref}: footprint library {nick!r} not in fp-lib-table")
        fp = pcbnew.FootprintLoad(lib_dir, fp_name)
        if fp is None:
            raise RuntimeError(f"{ref}: footprint {comp['footprint']!r} not found in {lib_dir}")
        if existing is not None:
            fp.SetPosition(existing.GetPosition())
            fp.SetOrientation(existing.GetOrientation())
            if existing.IsFlipped():
                fp.Flip(existing.GetPosition(), False)
            for m in existing.Models():
                cm = pcbnew.FP_3DMODEL(); cm.m_Filename = m.m_Filename; fp.Add3DModel(cm)
            board.Remove(existing)
            updated += 1
        else:
            fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(100.0 + (idx % 4) * 25.0), pcbnew.FromMM(100.0 + (idx // 4) * 25.0)))
            added += 1
        board.Add(fp)
    else:
        fp = existing
        updated += 1
    if comp['footprint']:
        fp.SetFPIDAsString(comp['footprint'])
    fp.SetReference(ref)
    fp.SetValue(comp['value'])
    for k, v in comp['fields'].items():
        fp.SetField(k, v)
    if hasattr(fp, 'SetDNP'):
        fp.SetDNP(bool(comp['dnp']))
    if hasattr(fp, 'SetExcludedFromBOM'):
        fp.SetExcludedFromBOM(bool(comp['exclude_from_bom']))
    for pad in fp.Pads():
        target = pad_net.get((ref, pad.GetNumber()))
        if target and target in net_map:
            pad.SetNet(net_map[target])
        else:
            pad.SetNetCode(0)
if spec['prune']:
    keep = {c['ref'] for c in spec['components']}
    for fp in list(board.GetFootprints()):
        if fp.GetReference() not in keep:
            board.Remove(fp)
pcb_file.parent.mkdir(parents=True, exist_ok=True)
pcbnew.SaveBoard(str(pcb_file), board)
print(json.dumps({'added': added, 'updated': updated, 'nets': len(spec['nets'])}))
"""


def sync_pcb(
    root: pathlib.Path,
    *,
    sch_path: pathlib.Path | None = None,
    pcb_path: pathlib.Path | None = None,
    replace_footprints: bool = False,
    prune: bool = False,
    export_bom: bool = False,
    board_description_path: pathlib.Path | None = None,
) -> dict:
    """Export schematic netlist via kicad-cli and sync footprints, fields, and nets into PCB offline."""
    desc = board_description.load(board_description_path, root=root) if board_description_path else board_description.load(root=root)
    sch = (sch_path or desc.path("schematic")).resolve()
    pcb = (pcb_path or desc.path("pcb")).resolve()
    table = desc.path("footprint_table").resolve()
    fp_lib = desc.path("footprint_library").resolve()
    kicad_py = board_sync_models._kicad_python()
    if kicad_py is None:
        raise RuntimeError("KiCad Python not found; cannot run offline pcbnew sync")
    invoke_cli = TOOL_ROOT / "tools" / "kicad" / "invoke_kicad_cli.py"
    if not invoke_cli.is_file():
        raise RuntimeError(f"missing {invoke_cli}")

    with tempfile.TemporaryDirectory() as tmp:
        net_file = pathlib.Path(tmp) / "netlist.net"
        cli_cmd = [sys.executable, "-B", str(invoke_cli), "sch", "export", "netlist", "--format", "kicadsexpr", "--output", str(net_file), str(sch)]
        cli_done = subprocess.run(cli_cmd, cwd=str(root), capture_output=True, text=True, check=False, **NO_WINDOW)
        if cli_done.returncode != 0 or not net_file.is_file():
            raise RuntimeError(cli_done.stderr.strip() or cli_done.stdout.strip() or "netlist export failed")
        parsed = parse_netlist(net_file.read_text(encoding="utf-8"), sch.read_text(encoding="utf-8"))
        libs = parse_fp_lib_table(table, pcb.parent, default_lib=(fp_lib.stem, fp_lib))
        spec = {
            "pcb_path": str(pcb),
            "copper_layers": _project_copper_layers(pcb),
            "components": parsed["components"],
            "nets": parsed["nets"],
            "lib_dirs": {k: str(v) for k, v in libs.items()},
            "replace_footprints": replace_footprints,
            "prune": prune,
        }
        spec_file = pathlib.Path(tmp) / "sync_spec.json"
        spec_file.write_text(json.dumps(spec), encoding="utf-8")
        done = subprocess.run([str(kicad_py), "-B", "-c", PCB_SYNC_WORKER, str(spec_file)], capture_output=True, text=True, check=False, **NO_WINDOW)
        if done.returncode != 0:
            raise RuntimeError(done.stderr.strip() or done.stdout.strip() or f"pcbnew sync failed (exit {done.returncode})")
        summary = json.loads(done.stdout.strip().splitlines()[-1])

    if export_bom:
        bom = board_bom_export.export_bom(root, check_pcb=True, board_description_path=board_description_path)
        summary["bom"] = {"fitted_components": bom["fitted_components"], "pcb_mismatches": bom["pcb_mismatches"]}
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sch", type=pathlib.Path, help="Input .kicad_sch file")
    parser.add_argument("--board", type=pathlib.Path, help="Output .kicad_pcb file")
    parser.add_argument("--replace-footprints", action="store_true", help="Reload matching footprints from library")
    parser.add_argument("--prune", action="store_true", help="Remove PCB footprints absent from schematic")
    parser.add_argument("--export-bom", action="store_true", help="Also run board-graph export-bom --check-pcb")
    parser.add_argument("--json", action="store_true", help="Print JSON summary")
    board_description.add_argument(parser)
    args = parser.parse_args(argv)

    try:
        summary = sync_pcb(
            ROOT,
            sch_path=args.sch,
            pcb_path=args.board,
            replace_footprints=args.replace_footprints,
            prune=args.prune,
            export_bom=args.export_bom,
            board_description_path=args.board_description,
        )
    except (OSError, ValueError, RuntimeError) as error:
        print(f"board-graph sync-pcb: {error}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"synced PCB: added={summary['added']} updated={summary['updated']} nets={summary['nets']}")
    return 1 if summary.get("bom", {}).get("pcb_mismatches") else 0


if __name__ == "__main__":
    raise SystemExit(main())
