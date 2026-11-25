#!/usr/bin/env python3
"""The scripts `board_graph.py next` runs: one function per node action, offline, without KiCad, except that
the footprints script runs KiCad's command-line tool on a disposable copy of the project, the ERC script runs it
on the saved schematic, and the board checks script reads the validate run's result for the commit from the
graph's database. The Stock, Verification and Package scripts read what their cards leave in the project: the
dated listings, the stamps on the check reports, and the sealed package.

Each takes the project root and returns {"outcome": "pass" | "needs_lead", "findings": [str, ...],
"detail": {...}}. The function decides the outcome. What a script says plainly that is not a finding goes in
detail["notes"], a list of sentences `next` adds to the record's note. Each action also names the project paths
it reads; `next` hashes them with the node's owned files and the tool's manifest, so a run on inputs that have
not changed, by the same version of the tool, is noticed.
The checks read the saved project through `Project`; tests change a `Project` in memory to make a case.
"""
from __future__ import annotations

import collections
import csv
import datetime
import functools
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import tempfile

import board_copper_origin
import board_description
import board_review_inputs
import boards
import kicad_rules_geom
import routing_evidence
import tool_paths
from graph_pack import PROJECT_ACTIONS, Action, Gate, Pack, file_sha256, project_actions, sha256  # noqa: F401
from kicad_sexp import child, children, normalize_angles, parse, prop
from no_window import KWARGS as NO_WINDOW

TOOL_ROOT = tool_paths.TOOL_ROOT

# The board's files, relative to the project root, from its description (board_description.py).
DESCRIPTION = board_description.load()
GRAPH = boards.chosen().graph
SCHEMATIC = DESCRIPTION.rel("schematic")
BOARD = DESCRIPTION.rel("pcb")
BOM = DESCRIPTION.rel("bom_export")
PLACEMENT = DESCRIPTION.rel("owner_placement")
PARTS = DESCRIPTION.rel("parts_data")
DATASHEETS = DESCRIPTION.rel("datasheets")
EXCLUSIONS = DESCRIPTION.rel("drc_exclusions")
LIBRARY = DESCRIPTION.library
PROJECT_FILE = DESCRIPTION.rel("kicad_project")
PROJECT_DIR = DESCRIPTION.project_folder
TABLE = DESCRIPTION.rel("footprint_table")
LIBRARY_DIR = DESCRIPTION.rel("footprint_library")
# DRC's two library checks: what each says about a board footprint when it fires.
LIBRARY_CHECKS = {"lib_footprint_mismatch": "differs from its library copy",
                  "lib_footprint_issues": "is not found in its library"}
TOOLS = TOOL_ROOT / "tools" / "kicad"
LIBRARY_HEIGHT_MM = 0.3  # a model's top may sit this far from its datasheet height
MODEL_DIRS = DESCRIPTION.rels("model_folders")

RULES = DESCRIPTION.rel("rules")
REPORTS = DESCRIPTION.rel("reports")
DESIGN = (SCHEMATIC, BOARD, PROJECT_FILE, RULES, BOM)
PRODUCTION = DESCRIPTION.rel("production")
PACKAGE_FOLDER = DESCRIPTION.rel("folder")  # where the complete package and its checksum are written
# What the Stock, Verification and Package cards' scripts read, each under its card's reports folder.
STOCK_LISTINGS = f"{REPORTS}/stock/listings.json"
LISTING_AGE_DAYS = 30  # a listing older than this no longer says what the factory has
VERIFICATION_CHECKS = f"{REPORTS}/verification/checks"
EXCLUSIONS_STAMPS = (pathlib.PurePosixPath(EXCLUSIONS).parent / "provenance.json").as_posix()
# What a new board does not have until its schematic is far enough along, in the words the cards say it in: Inventory
# names each one that is not there yet (baseline), and a later card that reads one refuses without it (not_there_yet).
MADE_LATER = {BOARD: "the board, which the first update from the schematic makes",
              RULES: "the board's rules file",
              BOM: "the exported BOM, which the first BOM export from the schematic writes"}
BOM_COLUMNS = ("Refs", "Qty", "Value", "Manufacturer", "MPN", "Supplier", "Supplier Part")
BOM_FIELDS = (("Value", "Value"), ("Manufacturer", "Manufacturer"), ("MPN", "Manufacturer Part"),
              ("Supplier", "Supplier"), ("Supplier Part", "Supplier Part"))
TARGET_FIELDS = {"manufacturer": "Manufacturer", "mpn": "Manufacturer Part", "supplier": "Supplier",
                 "supplier_part": "Supplier Part"}
LIBRARY_READS = (GRAPH, SCHEMATIC, BOARD, BOM, DATASHEETS, *MODEL_DIRS, LIBRARY_DIR, EXCLUSIONS)
FOOTPRINT_READS = (BOARD, PROJECT_FILE, TABLE, LIBRARY_DIR, EXCLUSIONS, PARTS, "scripts/board_manual_joints.py")


# A card's view of the board: what it reads, hashed, so the card passes by itself when that view is unchanged
# since its last pass. A view leaves out what the card never reads.
def placement_view(text):
    """Physical placement, including the mating envelope and the edge it mates with."""
    rows = []
    board = kicad_rules_geom.normalize_numeric_tokens(normalize_angles(parse(text)))
    edges = [[field for field in item if not isinstance(field, list) or field[0] not in ("uuid", "tstamp")]
             for item in board[1:] if isinstance(item, list) and child(item, "layer") == ["layer", "Edge.Cuts"]]
    for fp in children(board, "footprint"):
        at, layer = child(fp, "at"), child(fp, "layer")
        physical = [part for part in fp[1:] if isinstance(part, list) and
                    (part[0] in ("pad", "model") or (part[0].startswith("fp_") and part[0] != "fp_text"
                     and (child(part, "layer") or [None, None])[1] in ("Edge.Cuts", "F.Fab", "B.Fab", "F.CrtYd", "B.CrtYd")))]
        # Electrical net assignments do not change where a pad mates.
        physical = [[field for field in part if not isinstance(field, list) or field[0] not in
                     ("uuid", "tstamp", "net", "net_name", "pintype", "pinfunction")] for part in physical]
        rows.append([prop(fp, "Reference"), fp[1], layer[1] if layer else None,
                     at[1:4] if at else None, sorted(physical, key=json.dumps)])
    rows.sort(key=json.dumps)
    return sha256(json.dumps([rows, sorted(edges, key=json.dumps)], ensure_ascii=False).encode("utf-8"))


def copper_and_silk_view(text):
    """The saved board without its part fields: every footprint property except Reference and Value, which are
    drawn, and the file's version and generator lines are left out; copper, vias, zones and their fills,
    graphics, text, rule areas, the outline, the nets and the setup all stay."""
    kept = []
    for item in normalize_angles(parse(text))[1:]:
        if isinstance(item, list) and item and item[0] in ("version", "generator", "generator_version"):
            continue
        if isinstance(item, list) and item and item[0] == "footprint":
            item = [part for part in item if not (isinstance(part, list) and len(part) > 1 and part[0] == "property"
                                                   and part[1] not in ("Reference", "Value"))]
        kept.append(item)
    return sha256(json.dumps(kept, ensure_ascii=False).encode("utf-8"))


def graphic_layer(item):
    layer = child(item, "layer")
    return layer[1] if layer and len(layer) > 1 else ""


def graphic_without_id(item):
    return [part for part in item if not isinstance(part, list) or not part or part[0] not in ("uuid", "tstamp")]


def copper_view(text):
    board = normalize_angles(parse(text))
    kept = []
    for item in board[1:]:
        if not isinstance(item, list) or not item:
            continue
        if item[0] in ("version", "generator", "generator_version") or graphic_layer(item).endswith(".SilkS"):
            continue
        if item[0] == "footprint":
            fp = []
            for part in item:
                if isinstance(part, list):
                    layer = graphic_layer(part)
                    if layer.endswith(".SilkS"):
                        continue
                    if part[0] in ("property", "fp_text"):
                        if not layer.endswith(".Cu"):
                            continue
                        part = graphic_without_id(part)
                fp.append(part)
            item = [*fp, ["reference", prop(item, "Reference")]]
        kept.append(item)
    return sha256(json.dumps(kept, ensure_ascii=False).encode("utf-8"))


def silkscreen_view(text):
    """Printed geometry and the board features that can clip it."""
    rows = []
    for item in normalize_angles(parse(text))[1:]:
        if not isinstance(item, list) or not item:
            continue
        if item[0] == "footprint":
            marks = [graphic_without_id(part) for part in item if isinstance(part, list) and
                     (graphic_layer(part).endswith((".SilkS", ".Mask")) or part[0] == "pad")]
            # A printed field can refer to another property, e.g. ${Manufacturer}.
            printed = json.dumps(marks, ensure_ascii=False)
            fields = [(field[1], field[2]) for field in children(item, "property") if len(field) > 2
                      and (field[1] in ("Reference", "Value") or "${" + field[1] + "}" in printed)]
            rows.append([fields, child(item, "at"), graphic_layer(item), child(item, "solder_mask_margin"), marks])
        elif graphic_layer(item).endswith((".SilkS", ".Mask")) or graphic_layer(item) == "Edge.Cuts":
            rows.append(graphic_without_id(item))
        elif item[0] == "via":
            rows.append([child(item, key) for key in ("at", "size", "drill", "layers", "tenting", "covering", "plugging")])
        elif item[0] in ("setup", "embedded_files", "embedded_fonts"):
            rows.append(item)
    return sha256(json.dumps(rows, ensure_ascii=False).encode("utf-8"))


def purchasing_view(text):
    """Board inputs used for part identity, assembly side, fit status and hand-soldered pins."""
    rows = [kicad_rules_geom.purchasing_footprint_row(fp) for fp in children(parse(text), "footprint")]
    return sha256(json.dumps(sorted(rows, key=json.dumps), ensure_ascii=False).encode("utf-8"))


VIEWS = {
    "copper": (copper_view, "routing geometry, electrical setup and constraints"),
    "silkscreen": (silkscreen_view, "printed marks, placement, mask and board edge"),
    "purchasing": (purchasing_view, "board part identities, assembly flags, sides and pin types"),
    "placement": (placement_view, "the board edge and footprints' positions, sides and mating geometry"),
    "copper_and_silk": (copper_and_silk_view, "the board without its part fields: copper, zones, graphics, text, "
                                              "rule areas and outline"),
}


def view_hash(root, name):
    """The named view of the saved board under `root`."""
    return VIEWS[name][0]((pathlib.Path(root) / BOARD).read_text(encoding="utf-8"))


def text_sha256(path):
    """A text file's hash with LF line ends, so a checkout that writes CRLF hashes the same."""
    return sha256(pathlib.Path(path).read_bytes().replace(b"\r\n", b"\n"))


def read_json(path):
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8-sig"))


def refs(text):
    """The references a BOM row names: "C7-C9,C15" is C7, C8, C9 and C15."""
    result = []
    for token in text.split(","):
        token = token.strip()
        match = re.fullmatch(r"([A-Za-z]+)(\d+)-(?:([A-Za-z]+))?(\d+)", token)
        if match:
            prefix, start, other, end = match.groups()
            if (other and other != prefix) or int(end) < int(start):
                raise ValueError(f"Invalid reference range: {token}")
            result.extend(f"{prefix}{n}" for n in range(int(start), int(end) + 1))
        elif re.fullmatch(r"[A-Za-z]+\d+", token):
            result.append(token)
        else:
            raise ValueError(f"Invalid reference: {token}")
    return result


def manufacturer_name(value):
    """Ignore case and local-language parentheticals, not a different manufacturer."""
    return re.sub(r"\s*\([^()]*\)\s*", "", value or "").strip().casefold()


def fitted(symbol):
    return child(symbol, "in_bom") != ["in_bom", "no"] and child(symbol, "dnp") != ["dnp", "yes"]


def no_parts(footprints):
    """True for a board that carries copper shapes and no part: it has footprints, and the board keeps every one
    of them out of the BOM (a flexible strip whose only footprints are its contact tabs). Such a board has no
    BOM row, no factory part and nothing the owner places, so the cards that read those have nothing to compare.
    A board file with no footprint at all is not this: it is a board the schematic has not been put on yet."""
    footprints = list(footprints)
    return bool(footprints) and all("exclude_from_bom" in (child(fp, "attr") or []) for fp in footprints)


class Project:
    """The saved project as the checks read it. Each part is read when a check first asks for it."""

    def __init__(self, root):
        self.root = pathlib.Path(root).resolve()

    @functools.cached_property
    def graph(self):
        return read_json(self.root / GRAPH)

    @functools.cached_property
    def board(self):
        return parse((self.root / BOARD).read_text(encoding="utf-8"))

    @functools.cached_property
    def fps(self):
        return {prop(fp, "Reference"): fp for fp in children(self.board, "footprint")}

    @functools.cached_property
    def no_parts(self):
        return no_parts(self.fps.values())

    @functools.cached_property
    def project_file(self):
        return read_json(self.root / PROJECT_FILE)

    @functools.cached_property
    def library(self):
        """The project footprint library's footprints by name."""
        folder = self.root / LIBRARY_DIR
        return {path.stem: parse(path.read_text(encoding="utf-8-sig")) for path in sorted(folder.glob("*.kicad_mod"))}

    @functools.cached_property
    def symbols(self):
        sch = parse((self.root / SCHEMATIC).read_text(encoding="utf-8"))
        return {prop(s, "Reference"): s for s in children(sch, "symbol")
                if prop(s, "Reference") and not prop(s, "Reference").startswith("#")}

    def approved_targets(self):
        node = next((node for node in self.graph["nodes"] if node["id"] == "bom"), {})
        return node.get("approved_targets", [])

    def open_issues(self):
        return {issue["id"] for issue in self.graph.get("open_issues", [])}

    def bom_rows(self):
        with (self.root / BOM).open(encoding="utf-8-sig", newline="") as stream:
            return list(csv.DictReader(stream))

    def inventory(self):
        return {"board_sha256": text_sha256(self.root / BOARD), "footprints": len(self.fps),
                "segments": len(children(self.board, "segment")), "vias": len(children(self.board, "via")),
                "zones": len(children(self.board, "zone"))}

    def datasheet_path(self, name):
        if not isinstance(name, str) or pathlib.Path(name).is_absolute():
            raise ValueError(f"Datasheet paths must be relative to {DATASHEETS}")
        base = (self.root / DATASHEETS).resolve()
        path = (base / name).resolve()
        if not path.is_relative_to(base):
            raise ValueError(f"Datasheet path leaves {DATASHEETS}")
        return path

    def model_files(self):
        """Each saved model reference, resolved only inside the project's model folders and KiCad's own."""
        import board_space

        board_dir = (self.root / PROJECT_DIR).resolve()
        project_models = [(self.root / name).resolve() for name in MODEL_DIRS]
        # KiCad's own models are trusted only where KiCad 10 installs them, whatever the variable says.
        allowed = [*project_models, *(path.resolve() for path in board_space.STOCK_MODEL_INSTALLS)]
        result = {}
        for fp in self.fps.values():
            model = child(fp, "model")
            if not model:
                continue
            reference = str(model[1])
            if reference.startswith("${KIPRJMOD}/"):
                target = pathlib.Path(reference.replace("${KIPRJMOD}", str(board_dir))).resolve()
                if not any(target.is_relative_to(base) for base in project_models):
                    raise ValueError(f"Model path leaves the project's model directories: {reference}")
            elif reference.startswith("${KICAD10_3DMODEL_DIR}/"):
                relative = reference.split("}", 1)[1].lstrip("/")
                choices = [(pathlib.Path(base) / relative).resolve() for base in board_space.STOCK_MODELS]
                target = next((path for path in choices if path.exists()), None)
                if target is not None and not any(target.is_relative_to(base) for base in allowed):
                    raise ValueError(f"Stock model leaves trusted roots: {reference}")
            else:
                raise ValueError(f"Unsupported saved model path: {reference}")
            if target is not None and target.suffix.lower() not in (".step", ".stp"):
                raise ValueError(f"Unsupported model file type: {reference}")
            if target is not None and target.exists() and not target.is_file():
                raise ValueError(f"Model is not a regular file: {reference}")
            result[reference] = target if target is not None and target.is_file() else None
        return dict(sorted(result.items()))

    def model_dependencies(self):
        """Each resolved model bound to the exact bytes the model check reads."""
        return {name: file_sha256(path) if path is not None else None for name, path in self.model_files().items()}


SIDES = {"F.Cu": "front", "B.Cu": "back"}


def turn_apart(a, b):
    """How far apart two rotations are, in degrees from 0 to 180, after normalising both to 0 up to 360: 359.9995
    and 0 are 0.0005 apart, the same way a part turned that far faces."""
    apart = abs(float(a) % 360.0 - float(b) % 360.0)
    return min(apart, 360.0 - apart)


def plain(value):
    """A number as the findings say it: to a millionth, without trailing zeros."""
    return f"{float(value):.6f}".rstrip("0").rstrip(".")


def degrees(value):
    """A rotation as the findings say it: normalised to 0 up to 360."""
    return plain(float(value) % 360.0)


def placement_findings(project):
    """Each part in the owner's approved placement sits where it was approved, turned the way it was approved and on
    the side it was approved: x and y to 0.001 mm, rotation to 0.001 degree (turn_apart), side front or back. A
    listed part without a rotation or a side in the file is a finding, never a default."""
    approved = read_json(project.root / PLACEMENT)
    findings = []
    for ref, target in approved["parts"].items():
        fp = project.fps.get(ref)
        if fp is None:
            findings.append(f"{ref}: is not on the board")
            continue
        missing = [key for key in ("rotation", "side") if key not in target]
        if missing:
            findings.append(f"{ref}: the approved placement holds no {' and no '.join(missing)}, so it cannot be "
                            f"checked")
        at, layer = child(fp, "at"), child(fp, "layer")
        x, y = float(at[1]), float(at[2])
        if abs(x - target["x"]) > 0.001 or abs(y - target["y"]) > 0.001:
            findings.append(f"{ref}: at ({plain(x)}, {plain(y)}) mm, approved at ({plain(target['x'])}, "
                            f"{plain(target['y'])}) mm")
        rotation = at[3] if len(at) > 3 else 0
        if "rotation" in target and turn_apart(rotation, target["rotation"]) > 0.001:
            findings.append(f"{ref}: turned to {degrees(rotation)} degrees, approved at {degrees(target['rotation'])} "
                            f"degrees")
        side = SIDES.get(layer[1] if layer else None, layer[1] if layer else "no layer")
        if "side" in target and side != target["side"]:
            findings.append(f"{ref}: on the {side}, approved on the {target['side']}")
    return findings


def lcsc_field_findings(parts):
    """Each footprint's LCSC Part # names the same part as its Supplier Part.

    `parts` holds (reference, factory, LCSC Part #, Supplier Part) for each footprint; factory means fitted and
    marked SMD on the board. LCSC Part # is an EasyEDA-import field that only the board carries, and the JLC order
    generator reads it first, so a schematic part change that leaves it behind would order the old part. Where
    both fields are set they must agree, and a factory part needs both. `bom_report` and
    `kicad_generate_jlc_source_files.py` both apply this rule."""
    findings = []
    for ref, factory, lcsc, supplier in parts:
        lcsc, supplier = (lcsc or "").strip(), (supplier or "").strip()
        if lcsc and supplier and lcsc != supplier:
            findings.append(f"{ref}: board LCSC Part # {lcsc} differs from Supplier Part {supplier}")
        elif factory and lcsc and not supplier:
            findings.append(f"{ref}: factory part has LCSC Part # {lcsc} and no Supplier Part")
        elif factory and supplier and not lcsc:
            findings.append(f"{ref}: factory part has Supplier Part {supplier} and no LCSC Part #")
    return findings


def bom_report(project):
    """Schematic, board and exported BOM name the same fitted parts, every approved target is fitted with its
    selected identity, and every footprint's LCSC Part # agrees with its Supplier Part (lcsc_field_findings).
    Each row also says how it is assembled: factory SMT on which side, or manual, and the plated through-hole
    joints the owner solders on a factory SMT part."""
    findings, rows, seen = [], [], set()
    parts = {ref: symbol for ref, symbol in project.symbols.items() if fitted(symbol)}
    targets_seen = set()
    for target in project.approved_targets():
        for ref in refs(target["refs"]):
            if ref in targets_seen:
                findings.append(f"{ref}: duplicate approved target")
            targets_seen.add(ref)
            symbol = parts.get(ref)
            if symbol is None:
                findings.append(f"{ref}: approved target is missing or not fitted")
                continue
            for field, name in TARGET_FIELDS.items():
                actual = prop(symbol, name)
                same = (manufacturer_name(actual) == manufacturer_name(target[field]) if field == "manufacturer"
                        else actual == target[field])
                if not same:
                    findings.append(f"{ref}: approved {field} is {target[field]}, schematic has {actual or 'none'}")
    for row in project.bom_rows():
        references = refs(row["Refs"])
        if len(references) != int(row["Qty"]):
            findings.append(f"{row['Refs']}: quantity does not match references")
        roles, factory_sides, manual_through_hole = set(), set(), []
        for ref in references:
            duplicate = ref in seen
            if duplicate:
                findings.append(f"{ref}: duplicate BOM reference")
            seen.add(ref)
            symbol, fp = parts.get(ref), project.fps.get(ref)
            if symbol is None or fp is None:
                findings.append(f"{ref}: missing fitted schematic symbol or board footprint")
                continue
            for column, name in BOM_FIELDS:
                expected = prop(symbol, name) or ""
                if row[column] != expected:
                    findings.append(f"{ref}: exported {column} differs from schematic")
                if (prop(fp, name) or "") != expected:
                    findings.append(f"{ref}: board {column} differs from schematic")
            attr = child(fp, "attr") or []
            if any(flag in attr for flag in ("exclude_from_bom", "dnp")):
                findings.append(f"{ref}: fitted schematic conflicts with board BOM/DNP flags")
            roles.add("factory SMT" if "smd" in attr else "manual")
            if "smd" in attr:
                factory_sides.add((child(fp, "layer") or [None, "unknown"])[1])
                pads = [pad[1] for pad in children(fp, "pad") if len(pad) > 2 and pad[2] == "thru_hole"]
                if pads and not duplicate:
                    manual_through_hole.append({"ref": ref, "pad_numbers": pads,
                                                "physical_joints_per_board": len(pads)})
            if "smd" in attr and not row["Supplier Part"]:
                findings.append(f"{ref}: factory part has no supplier identity")
        if not row["MPN"] or not row["Manufacturer"]:
            findings.append(f"{row['Refs']}: exact manufacturer/part identity incomplete")
        rows.append({"refs": row["Refs"], "mpn": row["MPN"], "assembly": ", ".join(sorted(roles)),
                     "factory_sides": sorted(factory_sides), "owner_manual_through_hole": manual_through_hole})
    if seen != set(parts):
        findings.append(f"BOM/schematic reference mismatch: missing={sorted(set(parts) - seen)}, "
                        f"extra={sorted(seen - set(parts))}")
    board_parts = {ref for ref, fp in project.fps.items()
                   if not any(flag in (child(fp, "attr") or []) for flag in ("exclude_from_bom", "dnp"))}
    if board_parts != set(parts):
        findings.append(f"Board/schematic fitted mismatch: board_only={sorted(board_parts - set(parts))}, "
                        f"schematic_only={sorted(set(parts) - board_parts)}")
    findings.extend(lcsc_field_findings(
        (ref, ref in board_parts and "smd" in (child(fp, "attr") or []), prop(fp, "LCSC Part #"),
         prop(fp, "Supplier Part"))
        for ref, fp in project.fps.items()))
    sides = {side for row in rows for side in row["factory_sides"]}
    # A board with no part (no_parts) has no factory part to be on a side; any other board needs exactly one.
    if len(sides) != 1 and not (project.no_parts and not rows and not parts):
        findings.append(f"Factory SMT must occupy one side: {sorted(sides)}")
    return {"findings": findings, "rows": rows, "fitted_components": len(parts)}


def datasheet_findings(project):
    """Every BOM part is in the parts data, its datasheet is in the datasheet folder, and a height is taken from it."""
    try:
        parts = read_json(project.root / PARTS)["parts"]
        rows = project.bom_rows()
    except (OSError, KeyError, ValueError) as error:
        return [f"datasheet check could not read its sources: {error}"]
    findings = []
    for row in rows:
        spec = parts.get(row["MPN"])
        if spec is None:
            findings.append(f"{row['MPN']} ({row['Refs']}): not in {PARTS}")
            continue
        sheet = spec.get("datasheet")
        try:
            on_hand = bool(sheet) and project.datasheet_path(sheet).is_file()
        except ValueError as error:
            findings.append(f"{row['MPN']} ({row['Refs']}): {error}")
            on_hand = None
        if on_hand is False:
            findings.append(f"{row['MPN']} ({row['Refs']}): datasheet not in {DATASHEETS}")
        if spec.get("height_mm") is None:
            findings.append(f"{row['MPN']} ({row['Refs']}): no height taken from its datasheet")
    return findings


def model_findings(project, model_rows=None):
    """Every BOM part's 3D model loads and its top is within LIBRARY_HEIGHT_MM of its datasheet height.

    Saved model paths are checked before any model file is read. A height the owner left to an open issue
    (a measurement) is not compared while that issue stays open; closing the issue brings the comparison back.
    """
    import board_space

    try:
        project.model_files()
        parts = read_json(project.root / PARTS)["parts"]
        rows = project.bom_rows()
        models = {row["ref"]: row for row in (model_rows if model_rows is not None
                                              else board_space.report(project.root / BOARD))}
    except (OSError, KeyError, ValueError) as error:
        return [f"model check could not read its sources: {error}"]
    open_issues = project.open_issues()
    findings = []
    for row in rows:
        spec = parts.get(row["MPN"], {})
        height = spec.get("height_mm")
        for ref in refs(row["Refs"]):
            model = models.get(ref, {})
            if not model.get("model") or model.get("note") in ("model file missing", "no points read"):
                findings.append(f"{ref}: no 3D model that loads")
            elif "body_top_mm" not in model:
                findings.append(f"{ref}: model height not measured ({model.get('note', 'no height reported')})")
            elif (height is not None and spec.get("open_issue") not in open_issues
                  and abs(model["body_top_mm"] - height) > LIBRARY_HEIGHT_MM):
                findings.append(f"{ref}: model {model['body_top_mm']} mm high, datasheet {height} mm")
    return findings


def footprint_name(fp):
    """The library footprint a board footprint names, or None when it is not from the project library."""
    name = str(fp[1])
    return name.split(":", 1)[1] if name.startswith(LIBRARY + ":") else None


def models_of(fp):
    """A footprint's 3D models, which DRC's comparison leaves out: file, offset, rotation and scale."""
    return [(model[1], tuple(tuple(form[1][1:]) if form else None
                             for form in (child(model, key) for key in ("offset", "rotate", "scale"))))
            for model in children(fp, "model")]


def source_of(name, fps):
    """The part a shared footprint is written from: the one EasyEDA named it after, else the first on the board.
    Its 3D models are the library copy's; other parts on the footprint keep their own
    (scripts/kicad_footprints_to_library.py writes the library that way)."""
    return next((fp for fp in fps if name.endswith("__" + str(prop(fp, "Reference")))), fps[0])


def library_findings(project):
    """The parts of the Footprints node's check that need no KiCad: (findings, detail). DRC's accepted
    library-mismatch lists are empty (the documented list and the project file's exclusions), the project table
    maps the library, every board footprint names it and is in it, the library holds nothing else, and each library
    copy carries the 3D models of the part it is written from."""
    try:
        accepted = read_json(project.root / EXCLUSIONS)["accepted_warning_refs"]
        exclusions = project.project_file["board"]["design_settings"].get("drc_exclusions", [])
        table = parse((project.root / TABLE).read_text(encoding="utf-8-sig"))
    except (OSError, KeyError, ValueError) as error:
        return [f"footprint check could not read its sources: {error}"], {}
    findings = []
    if accepted:
        findings.append(f"board footprints accepted as differing from the library: {', '.join(accepted)}")
    by_uuid = {str((child(fp, "uuid") or [None, None])[1]): ref for ref, fp in project.fps.items()}
    excluded = [by_uuid.get(entry[0].split("|")[3], entry[0].split("|")[3]) for entry in exclusions
                if entry and str(entry[0]).startswith("lib_footprint_mismatch|") and entry[0].count("|") >= 3]
    if excluded:
        findings.append(f"{PROJECT_FILE} excludes library mismatches on: {', '.join(excluded)}")
    entries = [entry for entry in children(table, "lib") if child(entry, "name") == ["name", LIBRARY]]
    uri = "${KIPRJMOD}/" + LIBRARY + ".pretty"
    if not (len(entries) == 1 and child(entries[0], "type") == ["type", "KiCad"]
            and child(entries[0], "uri") == ["uri", uri]):
        findings.append(f"{TABLE} does not map {LIBRARY} to {uri}")
    used = {}
    for ref, fp in project.fps.items():
        name = footprint_name(fp)
        if name is None:
            findings.append(f"{ref}: footprint {fp[1]} is not from the project library")
        else:
            used.setdefault(name, []).append(fp)
    library, own_models = project.library, []
    for name, fps in sorted(used.items()):
        refs = [str(prop(fp, "Reference")) for fp in fps]
        if name not in library:
            findings.append(f"{name}: not in the library ({', '.join(refs)})")
            continue
        source = source_of(name, fps)
        if models_of(source) != models_of(library[name]):
            findings.append(f"{prop(source, 'Reference')}: 3D models differ from the library copy of {name}")
        own_models.extend({"ref": str(prop(fp, "Reference")), "footprint": name,
                           "models": [model[0] for model in models_of(fp)]}
                          for fp in fps if fp is not source and models_of(fp) != models_of(library[name]))
    unused = sorted(name for name in library if name not in used)
    if unused:
        findings.append(f"library footprints no board part uses: {', '.join(unused)}")
    folder = project.root / LIBRARY_DIR
    unexpected = sorted(path.relative_to(folder).as_posix() for path in folder.rglob("*")
                        if path.is_file() and (path.parent != folder or path.suffix != ".kicad_mod"))
    if unexpected:
        findings.append(f"files in the library that are not footprints: {', '.join(unexpected)}")
    detail = {"board_footprints": len(project.fps), "library_footprints": len(library),
              "shared": {name: [str(prop(fp, "Reference")) for fp in fps] for name, fps in sorted(used.items())
                         if len(fps) > 1},
              "own_models": own_models, "unused": unused, "unexpected_files": unexpected,
              "accepted_mismatches": list(accepted), "project_file_exclusions": excluded}
    return findings, detail


def footprint_findings(project):
    """library_findings() as the Datasheets and Models nodes carry it forward."""
    return library_findings(project)[0]


def joint_findings(project):
    """scripts/board_manual_joints.py on the saved board: (findings, detail). Every pin that does not fit its
    finished hole, land without mask on a face and solid plane setting, and every joint whose pin is not proven,
    so the Footprints card cannot pass while a hand-soldered joint is unproven."""
    import board_manual_joints

    try:
        report = board_manual_joints.report(project.root / BOARD, project.root / PARTS)
    except (OSError, KeyError, ValueError, TypeError, IndexError) as error:
        return [f"hand-soldered joint check could not read its sources: {error}"], {}
    findings = board_manual_joints.problems(report)
    findings += [f"not proven: {item}" for item in board_manual_joints.unproven(report)]
    return findings, {"counts": report["counts"], "tolerance": report["tolerance"],
                      "rules_note": report["rules_note"],
                      "rows": [{key: row[key] for key in ("part", "pads", "hole", "hole_finished_min", "least_ring_mm",
                                                          "mask_ok", "connection", "pin", "verdict", "margin_mm")}
                               for row in report["rows"]]}


def drc_library_findings(drc):
    """DRC's two library checks read from a report: (findings, detail). Every other violation on the copy goes in
    the detail, named for the CI gate that decides it."""
    findings, flagged, other = [], {}, collections.defaultdict(list)
    for violation in drc.get("violations", []):
        kind = violation.get("type")
        refs = []
        for item in violation.get("items", []):
            match = re.match(r"Footprint (\S+)", item.get("description", ""))
            refs.append(match.group(1) if match else item.get("description", ""))
        if kind in LIBRARY_CHECKS:
            for ref in refs or ["?"]:
                findings.append(f"{ref}: {LIBRARY_CHECKS[kind]} ({kind})")
                flagged.setdefault(kind, []).append(ref)
        else:
            other[kind].append(", ".join(refs))
    missing = [check for check in LIBRARY_CHECKS if check in drc.get("ignored_checks", [])]
    if missing:
        findings.insert(0, f"DRC ignored {', '.join(missing)}, so the copy was not compared")
    return findings, {"kicad_version": drc.get("kicad_version"), "library_checks": flagged,
                      "other_violations_on_the_copy": dict(other),
                      "unconnected_items": len(drc.get("unconnected_items", []))}


def kicad_cli():
    """The installed KiCad command-line tool, or None."""
    if str(TOOLS) not in sys.path:
        sys.path.insert(0, str(TOOLS))
    from kicad_install import KICAD_CLI
    return KICAD_CLI if KICAD_CLI.is_file() else None


def library_comparison(project):
    """KiCad's own comparison of every board footprint with its library copy: DRC's library checks on a
    disposable copy of the project (tools/kicad/create_disposable_project_copy.py) with its exclusions emptied
    and both checks raised to errors, run through tools/kicad/invoke_kicad_cli.py. The project is never touched.
    Returns (findings, detail); raises when KiCad does not run."""
    if kicad_cli() is None:
        raise FileNotFoundError("KiCad's command-line tool is not installed here")
    from create_disposable_project_copy import copy_project
    from invoke_kicad_cli import run_kicad_cli
    stem = DESCRIPTION.stem
    with tempfile.TemporaryDirectory(prefix="board-graph-footprints-") as temp:
        copy = pathlib.Path(temp) / "project"
        manifest = copy_project(project.root / PROJECT_DIR, stem, copy, stem)
        settings_path = copy / f"{stem}.kicad_pro"
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        settings = data["board"]["design_settings"]
        emptied = len(settings.get("drc_exclusions", []))
        settings["drc_exclusions"] = []
        for check in LIBRARY_CHECKS:
            settings.setdefault("rule_severities", {})[check] = "error"
        settings_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        report = copy / "footprints-drc.json"
        done = run_kicad_cli(["pcb", "drc", "--format", "json", "--severity-all", "--output", report,
                              copy / f"{stem}.kicad_pcb"], capture_output=True)
        if done.returncode != 0 or not report.is_file():
            raise RuntimeError(f"KiCad DRC did not run on the disposable copy (exit {done.returncode}): "
                               f"{((done.stderr or '') + (done.stdout or ''))[-300:]}")
        drc = json.loads(report.read_text(encoding="utf-8-sig"))
    findings, detail = drc_library_findings(drc)
    detail.update(copied_files=len(manifest["files"]), exclusions_emptied_on_the_copy=emptied,
                  drc_command="kicad-cli pcb drc --format json --severity-all, no refill, no schematic parity")
    return findings, detail


NO_PARTS = "this board carries no part (every footprint on it is kept out of the BOM)"


def result(findings, detail):
    return {"outcome": "needs_lead" if findings else "pass", "findings": list(findings), "detail": detail}


def not_there_yet(root, *names):
    """A finding for each named file the project does not have, for a card that reads it: a new board has no board
    file, rules or exported BOM until its schematic is far enough along (MADE_LATER), and a card that needs one
    says so in a sentence instead of failing to open it."""
    return [f"{name} is not there yet: this card reads {MADE_LATER.get(name, 'it')}"
            for name in names if not (pathlib.Path(root) / name).exists()]


def baseline(root):
    """The saved schematic, board, rules and BOM read cleanly; the board's contents are counted.

    A board with a part on it needs all five files (DESIGN). Before that, the project is at the schematic stage
    (there is no board file, or the board file holds no footprint): the project file and the schematic must read
    cleanly, each of the board, the rules and the exported BOM that exists must read cleanly too, and each one
    that is not there yet is named in detail["not_yet"] and in the record's note, never passed over in silence.
    A board file that does not parse counts as a board, so every file is required and the finding stands.
    A board with no part (no_parts) still needs its exported BOM, with its columns; it may hold no row."""
    import kicad_sexp

    root = pathlib.Path(root)
    problems, read, footprints, bare = {}, {}, None, False
    for name in DESIGN:
        path = root / name
        try:
            read[name] = file_sha256(path)
            text = path.read_text(encoding="utf-8-sig")
            if name.endswith(".kicad_pcb"):
                on_board = children(kicad_sexp.parse(text), "footprint")
                footprints, bare = len(on_board), no_parts(on_board)
            elif name.endswith(".kicad_sch"):
                kicad_sexp.parse(text)
            elif name.endswith(".kicad_pro"):
                json.loads(text)
            elif name.endswith(".kicad_dru"):
                # A rules file is a row of top-level forms with # comment lines between them.
                kicad_sexp.parse("(" + "\n".join(line for line in text.splitlines()
                                                 if not line.lstrip().startswith("#")) + ")")
            else:
                rows = csv.DictReader(text.splitlines())
                missing = [column for column in BOM_COLUMNS if column not in (rows.fieldnames or [])]
                if missing:
                    raise ValueError(f"missing columns {', '.join(missing)}")
                if not list(rows) and not bare:
                    raise ValueError("no rows")
        except (OSError, UnicodeDecodeError, ValueError) as error:
            problems[name] = error
    no_board_file = not (root / BOARD).exists()
    schematic_stage = no_board_file or footprints == 0
    not_yet = [name for name in DESIGN if schematic_stage and name in MADE_LATER and name in problems
               and not (root / name).exists()]
    findings = [f"{name}: does not read cleanly: {problems[name]}" for name in DESIGN
                if name in problems and name not in not_yet]
    detail = {"check": "saved files read cleanly", "read": read,
              "board": Project(root).inventory() if not findings and not no_board_file else None,
              "stage": "schematic" if schematic_stage else "board",
              "not_yet": [{"file": name, "what": MADE_LATER[name]} for name in not_yet],
              "scope": "Inventory only: the files parse and are counted. Not ERC, DRC or hardware performance."}
    if bare and not findings:
        detail["notes"] = [NO_PARTS + ": its exported BOM holds no row"]
    if schematic_stage:
        detail["notes"] = ["schematic stage: there is no board file yet" if no_board_file else
                           f"schematic stage: {BOARD} holds no part yet"]
        if not_yet:
            detail["notes"].append("not there yet: " + "; ".join(f"{name} ({MADE_LATER[name]})" for name in not_yet)
                                   + ". The cards that read them refuse until they are there")
    return result(findings, detail)


def bom(root):
    """bom_report(): schematic, board and exported BOM name the same parts, approved targets are fitted, and the
    board's LCSC Part # agrees with its Supplier Part."""
    missing = not_there_yet(root, SCHEMATIC, BOARD, BOM)
    if missing:
        return result(missing, {})
    report = bom_report(Project(root))
    return result(report["findings"], {
        "grouped_rows": len(report["rows"]), "fitted_components": report["fitted_components"],
        "rows": report["rows"],
        "note": "Identity and consistency only. Stock, price and assembly eligibility are the Stock and Quote "
                "nodes' work."})


def datasheets(root):
    """Every BOM part is in parts.json with its datasheet on hand and a height taken from it."""
    missing = not_there_yet(root, BOARD, BOM)
    if missing:
        return result(missing, {})
    project = Project(root)
    return result(datasheet_findings(project), {"left_for_footprints": footprint_findings(project)})


def models(root):
    """Every part's 3D model loads and its top is within LIBRARY_HEIGHT_MM of the datasheet height."""
    missing = not_there_yet(root, BOARD, BOM)
    if missing:
        return result(missing, {})
    project = Project(root)
    findings = model_findings(project)
    try:
        loaded = project.model_dependencies()
    except (OSError, ValueError) as error:
        loaded = {"error": str(error)}
    return result(findings, {"height_tolerance_mm": LIBRARY_HEIGHT_MM, "model_sha256": loaded,
                             "left_for_footprints": footprint_findings(project)})


def footprints(root):
    """Every board footprint is identical to its library copy and the library holds nothing the board does not use:
    KiCad's own comparison (library_comparison(): DRC's library checks on a disposable copy, exclusions emptied),
    then library_findings(): the 3D models, the library's files and DRC's accepted library-mismatch lists, which
    must be empty; and joint_findings(): every hand-soldered joint's real pin fits its finished hole, its land is
    open on both faces and no plane setting is solid."""
    missing = not_there_yet(root, BOARD, PROJECT_FILE)
    if missing:
        return result(missing, {})
    project = Project(root)
    offline, detail = library_findings(project)
    joints, joint_detail = joint_findings(project)
    compared, drc_detail = library_comparison(project)
    return result(compared + offline + joints,
                  {**detail, **drc_detail, "manual_joints": joint_detail,
                   "scope": "Footprint identity with the project library and the library's contents, and the "
                            "hand-soldered joints against their datasheet pins. The other violations DRC raised on "
                            "the copy are CI's DRC gate to decide, not this node's."})


def placement(root):
    """placement_findings(): recorded footprint positions, rotations and sides match the owner's approved placement
    (the description's owner_placement)."""
    missing = not_there_yet(root, BOARD)
    if missing:
        return result(missing + not_there_yet(root, PLACEMENT), {})
    project = Project(root)
    if project.no_parts and not (project.root / PLACEMENT).exists():
        # Nothing on the board is the owner's to place, so there is no placement file to compare with. A file that
        # is there is still compared, below.
        return result([], {"parts_compared": 0,
                           "notes": [NO_PARTS + f": there is no part to place, so {PLACEMENT} is not needed"],
                           "scope": "Nothing compared. The owner's approval of a rendered view is still the "
                                    "card's pass."})
    missing = not_there_yet(root, PLACEMENT)
    if missing:
        return result(missing, {})
    approved = read_json(project.root / PLACEMENT)
    return result(placement_findings(project),
                  {"parts_compared": len(approved["parts"]),
                   "scope": "Recorded footprint positions, rotations and sides only; not the owner's approval of a "
                            "rendered view."})


BOARD_CHECK_READS = (*DESIGN, "scripts/validate_board.py", ".github/workflows/validate.yml")


def board_checks_findings(summary):
    """What keeps the Board checks card from passing, read from a validate run's summary.json, with the open
    count: a check that failed; one that passed only as inherited from the base commit; a run that skipped the
    checks (an incremental run on a change that touches nothing they read); and connections still open, under
    which the checks allow an unfinished board (unconnected items, pads waiting on routing, electrical findings by
    name), so none of them is a pass for this card."""
    checks = summary.get("checks") or []
    findings = []
    skipped = any(item["check"] == "Board checks" and item["ok"] for item in checks)
    if skipped or not checks:
        findings.append("the run skipped the checks (an incremental run on a change that touches nothing they "
                        "read): the full run is the one on main")
    for item in checks:
        if item.get("inherited"):
            findings.append(f"{item['check']}: {item['detail']}")
        elif not item["ok"]:
            findings.append(f"{item['check']}: FAIL, {item['detail']}")
    open_count = next((item["open"] for item in checks if "open" in item), None)
    if open_count:
        findings.append(f"{open_count} connection{'s' if open_count != 1 else ''} still open: the checks allowed an "
                        "unfinished board (unconnected items, pads waiting on routing, electrical findings by "
                        "name), so none of them is a pass for this card")
    elif open_count is None and checks and not skipped:
        findings.append("DRC gave no usable unconnected-items count")
    return findings, open_count


def board_checks(root, lookup=None):
    """The board checks on this checkout's commit, as a validate run kept them on the graph with
    `board_graph.py checks`: a workflow's last step on the KiCad machine's runner, or scripts/run_board_checks.py
    run there by hand in a project without one. The newest row on the commit, and board_checks_findings() on it.
    No row yet, or a run that left no checks, is a finding, not a pass. `lookup` stands in for the database in
    tests; by default the one database, opened as every command opens it."""
    root = pathlib.Path(root)
    done = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(root), capture_output=True, check=False, **NO_WINDOW)
    if done.returncode != 0:
        raise RuntimeError(f"git rev-parse HEAD: {done.stderr.decode('utf-8', 'replace').strip()}")
    head = done.stdout.decode("ascii").strip()
    if lookup is None:
        import board_graph  # board_graph imports this module, so the import waits until the action runs
        lookup = board_graph.open_state(board_graph.place(), board_graph.RECORDS).checks_for
    row = lookup(head)
    detail = {"commit": head, "run": None, "pass": None, "open": None, "checks": [],
              "scope": "The board checks on this commit as a validate run kept them on the graph: "
                       "scripts/validate_board.py as the KiCad machine ran it (a workflow on its runner, or "
                       "scripts/run_board_checks.py there), not run here."}
    if row is None:
        return result([f"no board checks result on the graph for {head[:8]}: with a validate workflow, push it "
                       f"and run next when the run has finished; without one, run scripts/run_board_checks.py"
                       f"{boards.chosen(root).argument} on the KiCad machine in a checkout of this commit"], detail)
    detail["run"] = {"id": row["run_id"], "event": row["event"], "at": row["at"]}
    if row["passed"] is None:
        return result([f"validate run {row['run_id']} on {head[:8]} left no checks: "
                       f"{row.get('note') or 'no summary'}"], detail)
    findings, open_count = board_checks_findings({"pass": row["passed"], "checks": row["checks"]})
    detail.update({"pass": row["passed"], "open": open_count, "checks": row["checks"]})
    return result(findings, detail)


def erc_findings(report):
    """What KiCad's ERC report holds against a pass: every violation, errors and warnings alike, and a report
    that left a severity out."""
    findings = []
    if not {"error", "warning"} <= set(report.get("included_severities", [])):
        findings.append("the ERC report does not include both errors and warnings")
    for sheet in report.get("sheets", []):
        for violation in sheet.get("violations", []):
            where = "; ".join(item.get("description", "") for item in violation.get("items", [])
                              if item.get("description"))
            findings.append(f"{violation.get('severity', 'violation')}: {violation.get('description', '')} "
                            f"({violation.get('type', 'no type')})" + (f": {where}" if where else ""))
    return findings


def erc_report(root):
    """KiCad's ERC of the saved schematic, every severity, as its JSON report: run through
    tools/kicad/invoke_kicad_cli.py into a temporary folder, so the project is never written. Raises when KiCad
    does not run."""
    if kicad_cli() is None:
        raise FileNotFoundError("KiCad's command-line tool is not installed here")
    from invoke_kicad_cli import run_kicad_cli
    with tempfile.TemporaryDirectory(prefix="board-graph-erc-") as temp:
        report = pathlib.Path(temp) / "erc.json"
        done = run_kicad_cli(["sch", "erc", "--format", "json", "--severity-all", "--output", report,
                              pathlib.Path(root) / SCHEMATIC], capture_output=True)
        if done.returncode != 0 or not report.is_file():
            raise RuntimeError(f"KiCad ERC did not run on the schematic (exit {done.returncode}): "
                               f"{((done.stderr or '') + (done.stdout or ''))[-300:]}")
        return json.loads(report.read_text(encoding="utf-8-sig"))


def erc(root, run=erc_report):
    """erc_findings(): KiCad's ERC of the saved schematic reports 0 errors and 0 warnings. The critic's findings
    and how the pages read are the card's own work, not this script's."""
    missing = not_there_yet(root, SCHEMATIC)
    if missing:
        return result(missing, {})
    if run is erc_report and kicad_cli() is None:
        # Not a pass and not a crash: the card's script could not run here, and says where it can.
        return result(["KiCad's command-line tool is not installed here, so ERC was not run: start the card on a "
                       "machine that has KiCad"], {"kicad_version": None})
    report = run(root)
    findings = erc_findings(report)
    return result(findings, {"kicad_version": report.get("kicad_version"), "violations": len(findings),
                             "erc_command": "kicad-cli sch erc --format json --severity-all",
                             "scope": "KiCad's electrical rules check of the saved schematic only; not the "
                                      "schematic critic's findings and not how the pages read."})


def listing_time(value):
    """When a listing says it was read, as an aware time: a date or a date and time in ISO form; None otherwise."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        read = datetime.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return read if read.tzinfo else read.replace(tzinfo=datetime.timezone.utc)


def supply_findings(needed, listings, now):
    """What the dated listings hold against the Stock card's rule: (findings, notes).

    `needed` is the factory parts the board asks for, {supplier part: {"refs": [...], "mpn": str}}; `listings`
    the listings file as read. Every factory part needs a listing that names its supplier part, the listed
    manufacturer, MPN and class, a stock count and a price, read at most LISTING_AGE_DAYS days ago; the listed
    MPN is the board's; and a part with less stock than the planned boards need carries the plan the owner
    accepted, in the owner's words (`owner_plan`).
    """
    findings, notes = [], []
    boards_planned = listings.get("planning_quantity")
    if not isinstance(boards_planned, int) or isinstance(boards_planned, bool) or boards_planned < 1:
        findings.append("planning_quantity is missing or not a whole number of boards: stock is compared with "
                        "what that many boards need")
        boards_planned = None
    rows = listings.get("factory_parts")
    if not isinstance(rows, list):
        return findings + ["factory_parts is missing: the list of listings, one per factory part"], notes
    listed = {}
    for row in rows:
        part = row.get("part") if isinstance(row, dict) else None
        if not isinstance(part, str) or not part:
            findings.append("a listing names no part (its supplier part number)")
        elif part in listed:
            findings.append(f"{part}: listed twice")
        else:
            listed[part] = row
    for part, want in sorted(needed.items()):
        refs_text = ", ".join(want["refs"])
        row = listed.get(part)
        if row is None:
            findings.append(f"{part} ({refs_text}): no listing")
            continue
        for key in ("listed_manufacturer", "listed_mpn", "class"):
            if not isinstance(row.get(key), str) or not row[key].strip():
                findings.append(f"{part} ({refs_text}): the listing has no {key}")
        if isinstance(row.get("listed_mpn"), str) and want["mpn"] and row["listed_mpn"].strip() != want["mpn"]:
            findings.append(f"{part} ({refs_text}): listed as {row['listed_mpn'].strip()}, the board names "
                            f"{want['mpn']}")
        price = row.get("unit_price_usd")
        if isinstance(price, bool) or not isinstance(price, (int, float)):
            findings.append(f"{part} ({refs_text}): the listing has no unit_price_usd")
        read = listing_time(row.get("read_at"))
        if read is None:
            findings.append(f"{part} ({refs_text}): the listing has no read_at, the date it was read")
        elif read > now + datetime.timedelta(days=1):
            findings.append(f"{part} ({refs_text}): read_at {row['read_at']} is in the future")
        elif now - read > datetime.timedelta(days=LISTING_AGE_DAYS):
            findings.append(f"{part} ({refs_text}): read {(now - read).days} days ago, more than "
                            f"{LISTING_AGE_DAYS}")
        stock = row.get("stock")
        if isinstance(stock, bool) or not isinstance(stock, int) or stock < 0:
            findings.append(f"{part} ({refs_text}): the listing has no stock count")
        elif boards_planned is not None and stock < len(want["refs"]) * boards_planned:
            short = (f"{part} ({refs_text}): {stock} in stock, {boards_planned} boards need "
                     f"{len(want['refs']) * boards_planned}")
            plan = row.get("owner_plan")
            if isinstance(plan, str) and plan.strip():
                notes.append(f"{short}; the owner's plan: {plan.strip()}")
            else:
                findings.append(f"{short}, and the listing carries no owner_plan: the plan the owner accepted, "
                                f"in the owner's words")
    extra = sorted(set(listed) - set(needed))
    if extra:
        notes.append(f"listed but not a factory part on the board: {', '.join(extra)}")
    return findings, notes


def factory_parts(project):
    """The parts the factory places, by supplier part: the fitted footprints the board marks SMD. Returns
    ({supplier part: {"refs": [...], "mpn": str}}, the references that carry no supplier part)."""
    needed, unnamed = {}, []
    for ref, fp in sorted(project.fps.items()):
        attr = child(fp, "attr") or []
        if "smd" not in attr or any(flag in attr for flag in ("exclude_from_bom", "dnp")):
            continue
        part = (prop(fp, "Supplier Part") or "").strip()
        if not part:
            unnamed.append(ref)
            continue
        entry = needed.setdefault(part, {"refs": [], "mpn": (prop(fp, "Manufacturer Part") or "").strip()})
        entry["refs"].append(ref)
    return needed, unnamed


def supply_receipt(root, now=None):
    """supply_findings(): a dated listing, at most LISTING_AGE_DAYS days old, for every part the factory places,
    read from the Stock card's listings file (STOCK_LISTINGS), and the owner's accepted plan for every
    shortage."""
    missing = not_there_yet(root, BOARD)
    if missing:
        return result(missing, {})
    now = now or datetime.datetime.now(datetime.timezone.utc)
    project = Project(root)
    needed, unnamed = factory_parts(project)
    detail = {"listings": STOCK_LISTINGS, "factory_parts": len(needed),
              "factory_placements": sum(len(want["refs"]) for want in needed.values()),
              "oldest_allowed_days": LISTING_AGE_DAYS,
              "scope": "The listings file against the saved board: that each factory part has a dated listing "
                       "with stock and price. The script reads no catalog; whoever is on the card reads the "
                       "supplier and writes the file."}
    findings = [f"{ref}: a factory part with no Supplier Part, so no listing can name it" for ref in unnamed]
    path = pathlib.Path(root) / STOCK_LISTINGS
    if not path.is_file() and not needed and not unnamed and project.no_parts:
        detail["notes"] = [NO_PARTS + f": the factory places nothing, so {STOCK_LISTINGS} is not needed"]
        return result([], detail)
    if not path.is_file():
        return result(findings + [f"{STOCK_LISTINGS} is not there: the dated listings, one per factory part "
                                  f"({len(needed)} on this board)"], detail)
    try:
        listings = read_json(path)
    except ValueError as error:
        return result(findings + [f"{STOCK_LISTINGS} cannot be read: {error}"], detail)
    if not isinstance(listings, dict):
        return result(findings + [f"{STOCK_LISTINGS} is not an object with planning_quantity and factory_parts"],
                      detail)
    found, notes = supply_findings(needed, listings, now)
    detail.update(planning_quantity=listings.get("planning_quantity"), notes=notes)
    return result(findings + found, detail)


STAMP_WORDS = {"missing": "is not there", "unknown": "has no stamp", "stale": "is stamped against another board"}


def kicad_report_freshness_verdict(report, board):
    """One report's stamp against `board`: fresh, stale, unknown (no stamp) or missing."""
    import kicad_report_freshness
    if not report.is_file():
        return "missing"
    return kicad_report_freshness.classify(report.parent, board).get(report.name, "missing")


def stamp_findings(folder, board, required):
    """What the stamps in `folder` hold against `board`: a finding for each required report that is missing, not
    stamped or stamped against another board, and for any other report stamped against another board; a note
    for each other file with no stamp. Returns (findings, notes, {report: fresh | stale | unknown})."""
    import kicad_report_freshness
    if not folder.is_dir():
        return [f"{folder.name}/ is not there: no report is stamped"], [], {}
    verdicts = kicad_report_freshness.classify(folder, board)
    findings = [f"{name} {STAMP_WORDS[verdicts.get(name, 'missing')]}" for name in required
                if verdicts.get(name, "missing") != "fresh"]
    findings += [f"{name} is stamped against another board" for name, verdict in sorted(verdicts.items())
                 if verdict == "stale" and name not in required]
    notes = [f"{name} has no stamp" for name, verdict in sorted(verdicts.items())
             if verdict == "unknown" and name not in required]
    return findings, notes, verdicts


def gate_report_names():
    """The board checks' report files a package reads, as scripts/kicad_package_production.py names them."""
    from kicad_package_production import gate_reports
    return sorted(gate_reports(DESCRIPTION).values())


def freshness(root):
    """stamp_findings(): every board check report under the Verification card's checks folder
    (VERIFICATION_CHECKS) and the accepted-warnings list (the description's drc_exclusions) is stamped against
    the saved board and schematic (scripts/kicad_report_freshness.py)."""
    missing = not_there_yet(root, BOARD)
    if missing:
        return result(missing, {})
    root = pathlib.Path(root)
    board = root / BOARD
    checks, notes, verdicts = stamp_findings(root / VERIFICATION_CHECKS, board, gate_report_names())
    findings = [f"{VERIFICATION_CHECKS}: {line}" for line in checks]
    exclusions = root / EXCLUSIONS
    accepted = kicad_report_freshness_verdict(exclusions, board)
    if accepted != "fresh":
        findings.append(f"{EXCLUSIONS} {STAMP_WORDS[accepted]}")
    return result(findings,
                  {"reports": VERIFICATION_CHECKS, "board_sha256": file_sha256(board).upper(), "stamps": verdicts,
                   "notes": notes,
                   "scope": "The stamps only: that each report was stamped against this exact board and "
                            "schematic. What a report says, the open issues and the independent review are the "
                            "card's own work."})


def same_hash(a, b):
    return isinstance(a, str) and isinstance(b, str) and a.upper() == b.upper()


def package_findings(root):
    """What the sealed package in the production folder holds against a pass: (findings, detail).

    The packager (scripts/kicad_package_production.py) makes and seals the package on the KiCad machine and
    refuses unless every gate passes. This reads what it sealed: the package manifest names the saved board and
    schematic by hash; every file it lists is there, unchanged, and the folder holds no other; its gates read
    zero; the evidence it carries is stamped against this board and the independent review's first line is PASS;
    and the one complete package beside the board's folder matches its recorded checksum.
    """
    from kicad_package_production import CRITIC_REPORT, VISUAL_INSPECTION
    root = pathlib.Path(root)
    production, board, schematic = root / PRODUCTION, root / BOARD, root / SCHEMATIC
    manifest_path, assembly_path = production / "package-manifest.json", production / "assembly-manifest.json"
    detail = {"production": PRODUCTION, "board_sha256": file_sha256(board).upper()}
    absent = [path.name for path in (manifest_path, assembly_path) if not path.is_file()]
    if absent:
        return [f"{PRODUCTION} holds no sealed package ({', '.join(absent)} not there): make it on the KiCad "
                f"machine as the order package doc says, ending with scripts/kicad_package_production.py"], detail
    manifest, assembly = read_json(manifest_path), read_json(assembly_path)
    findings = []
    sources = manifest.get("design_sources", {})
    for name, path in (("pcb", board), ("schematic", schematic)):
        if not same_hash(sources.get(name, {}).get("sha256"), file_sha256(path)):
            findings.append(f"the package was made from another {'board' if name == 'pcb' else 'schematic'}: "
                            f"its manifest's hash is not {path.name}'s")
    if not same_hash(assembly.get("board_sha256"), file_sha256(board)):
        findings.append("the assembly files were checked against another board: assembly-manifest.json's hash "
                        f"is not {board.name}'s")
    listed = {}
    for entry in manifest.get("files", []):
        listed[entry.get("path")] = entry
        path = production / str(entry.get("path"))
        if not path.is_file():
            findings.append(f"{entry.get('path')}: listed in the package manifest, not in {PRODUCTION}")
        elif not same_hash(entry.get("sha256"), file_sha256(path)):
            findings.append(f"{entry.get('path')}: changed since the package was sealed")
    if not listed:
        findings.append("the package manifest lists no files")
    on_disk = {path.relative_to(production).as_posix() for path in production.rglob("*")
               if path.is_file() and "superseded" not in path.relative_to(production).parts}
    for name in sorted(on_disk - set(listed) - {manifest_path.name}):
        findings.append(f"{name}: in {PRODUCTION}, not in the package manifest (the folder holds only this "
                        f"package)")
    gates = manifest.get("gates", {})
    for key in ("erc_violations", "active_drc_violations", "schematic_parity_issues", "unconnected_items",
                "route_quality_errors", "pad_mask_findings"):
        if gates.get(key) != 0 or isinstance(gates.get(key), bool):
            findings.append(f"the package manifest's {key} is {gates.get(key)!r}, not 0")
    route_warnings = gates.get("route_quality_warnings")
    validation = production / "validation"
    if not isinstance(route_warnings, int) or isinstance(route_warnings, bool) or route_warnings < 0:
        findings.append(f"the package manifest's route_quality_warnings is {route_warnings!r}, not 0")
    elif route_warnings > 0:
        route_report_path = validation / "route-quality.json"
        try:
            route_report = read_json(route_report_path) if route_report_path.is_file() else None
        except (OSError, ValueError):
            route_report = None
        if not isinstance(route_report, dict) or (route_report.get("summary") or {}).get("warnings") != route_warnings:
            findings.append(f"the package manifest's route_quality_warnings is {route_warnings!r}, and "
                            f"{PRODUCTION}/validation/route-quality.json does not match it")
        else:
            search_dirs = routing_evidence.routing_receipt_dirs(root, BOARD, DESCRIPTION.routing_agent)
            accepted_map = routing_evidence.accepted_route_quality_warnings(root, BOARD, search_dirs)
            matched, unaccepted_items, unlisted = routing_evidence.classify_route_quality_warnings(
                route_report, accepted_map
            )
            recorded = gates.get("accepted_route_quality_warnings")
            recorded_details = (collections.Counter(item.get("detail") for item in recorded if isinstance(item, dict))
                                if isinstance(recorded, list) else None)
            matched_details = collections.Counter(item["detail"] for item in matched)
            if unaccepted_items or unlisted or len(matched) != route_warnings:
                findings.append(f"the package manifest's route_quality_warnings is {route_warnings!r} with "
                                f"{len(unaccepted_items) + unlisted} unreviewed warning(s) lacking verified routing receipts")
            elif recorded_details != matched_details:
                findings.append("the package manifest's accepted_route_quality_warnings does not match the "
                                "verified routing receipts for route-quality.json")
    if gates.get("accepted_library_warnings") != []:
        findings.append("the package manifest accepts library warnings: the reference list must be empty")
    if gates.get("visual_inspection") != "pass":
        findings.append("the package manifest records no passed visual inspection")
    if (gates.get("fabrication_matches_fresh_export") or {}).get("all_layers_match_fresh_export") is not True:
        findings.append("the package manifest does not record that every fabrication layer matched a fresh export")
    evidence = [*gate_report_names(), VISUAL_INSPECTION, CRITIC_REPORT]
    stamped, _, verdicts = stamp_findings(validation, board, evidence)
    findings += [f"{PRODUCTION}/validation: {line}" for line in stamped]
    critic = validation / CRITIC_REPORT
    if critic.is_file():
        first = (critic.read_text(encoding="utf-8").splitlines() or [""])[0]
        if not re.match(r"^PASS(?:\s*[—:\-]|\s*$)", first):
            findings.append(f"{CRITIC_REPORT}'s first line is not PASS: the independent review (G10) has not "
                            f"passed this package")
    folder = root / PACKAGE_FOLDER
    sums = sorted(folder.glob("*-complete-package.sha256"))
    detail.update(files=len(listed), stamps=verdicts, complete_package=None)
    if len(sums) != 1:
        findings.append(f"{PACKAGE_FOLDER} holds {len(sums)} complete-package checksum files, not exactly one")
    else:
        words = sums[0].read_text(encoding="utf-8").split()
        archive = folder / (words[1].lstrip("*") if len(words) > 1 else sums[0].with_suffix(".zip").name)
        if not archive.is_file():
            findings.append(f"{archive.name}: named by {sums[0].name}, not there")
        elif not words or not same_hash(words[0], file_sha256(archive)):
            findings.append(f"{archive.name} does not match the checksum in {sums[0].name}")
        else:
            detail["complete_package"] = {"file": f"{PACKAGE_FOLDER}/{archive.name}",
                                          "sha256": file_sha256(archive).upper()}
    return findings, detail


def package(root):
    """package_findings(): the production folder holds one sealed package made from the saved board and
    schematic, unchanged since it was sealed, with every gate at zero, its evidence stamped against this board,
    the independent review passed, and a complete package that matches its checksum."""
    missing = not_there_yet(root, BOARD, SCHEMATIC)
    if missing:
        return result(missing, {})
    findings, detail = package_findings(root)
    detail["scope"] = ("What the packager sealed, read without KiCad: hashes, the manifest's gates and the "
                       "stamps. The packager itself regenerates the fabrication files and compares them when it "
                       "seals; the owner's look at the assembler's preview (G11) is the next card.")
    return result(findings, detail)


ACTIONS = {
    "baseline": Action(baseline, DESIGN),
    "bom": Action(bom, (GRAPH, SCHEMATIC, BOARD, BOM)),
    "datasheets": Action(datasheets, LIBRARY_READS),
    "models": Action(models, LIBRARY_READS),
    "footprints": Action(footprints, FOOTPRINT_READS),
    "placement": Action(placement, (BOARD, PLACEMENT)),
    "board_checks": Action(board_checks, BOARD_CHECK_READS),
    "erc": Action(erc, (SCHEMATIC, PROJECT_FILE)),
    "supply_receipt": Action(supply_receipt, (BOARD, STOCK_LISTINGS)),
    "freshness": Action(freshness, (BOARD, SCHEMATIC, VERIFICATION_CHECKS, EXCLUSIONS, EXCLUSIONS_STAMPS)),
    "package": Action(package, (BOARD, SCHEMATIC, PRODUCTION)),
}


def check_shipping_pass(node, action, evidence, root, spec=None):
    """Check the release claim at record time, including after an agent produced the files."""
    if node == "verification" and action in (None, "freshness"):
        schema = "board-graph/independent-review/v1"
    elif node == "product_judgment":
        schema = "board-graph/product-acceptance/v1"
    else:
        return
    root = pathlib.Path(root).resolve()
    cited = {(root / name).resolve() for name in evidence}
    receipts = []
    for path in cited:
        if not path.is_relative_to(root) or not path.is_file() or path.suffix.lower() != ".json":
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeError):
            continue
        if isinstance(value, dict) and value.get("schema") == schema:
            receipts.append((path, value))
    if len(receipts) != 1:
        raise ValueError(f"{node} needs one cited {schema} receipt. See docs/BOARD_EVIDENCE.md.")
    receipt_path, receipt = receipts[0]

    def supported(check):
        if not isinstance(check, dict) or not isinstance(check.get("evidence"), str):
            return False
        path = (root / check["evidence"]).resolve()
        return (check.get("passed") is True and isinstance(check.get("observation"), str)
                and check["observation"].strip() and path in cited and path != receipt_path
                and path.is_relative_to(root) and path.is_file() and path.stat().st_size > 0)

    if schema.endswith("independent-review/v1"):
        for key, name in (("board_sha256", BOARD), ("schematic_sha256", SCHEMATIC)):
            source = root / name
            if not source.is_file() or str(receipt.get(key, "")).lower() != file_sha256(source).lower():
                raise ValueError(f"Independent review is missing or stale for {name}")
        people = [receipt.get(key) for key in ("builder", "reviewer")]
        if not all(isinstance(p, str) and p.strip() for p in people) or people[0].strip().casefold() == people[1].strip().casefold():
            raise ValueError("Independent review must identify the builder and a separate reviewer")
        checks = receipt.get("checks")
        if not isinstance(checks, list) or not checks or not all(supported(check) for check in checks):
            raise ValueError("Verification needs passing reproduced checks with observations and cited reports")
    else:
        boards = receipt.get("board_revisions")
        if (receipt.get("verdict") != "accepted" or not isinstance(boards, dict) or not boards
                or not all(k.strip() and isinstance(v, str) and v.strip() for k, v in boards.items())):
            raise ValueError("Product judgment requires an accepted verdict identifying the tested board revisions")
        contract = (spec or {}).get("integration", {})
        for name, board_path in contract.get("boards", {}).items():
            source = (root / board_path).resolve()
            if (not source.is_relative_to(root) or not source.is_file()
                    or boards.get(name) != file_sha256(source)):
                raise ValueError(f"First article needs the tested SHA-256 for board {name} at {board_path}")
        units = receipt.get("units")
        if not isinstance(units, list) or not units:
            raise ValueError("Product judgment requires the tested units")
        for unit in units:
            if (not isinstance(unit, dict) or not isinstance(unit.get("id"), str) or not unit["id"].strip()
                    or not isinstance(unit.get("firmware"), str) or not unit["firmware"].strip()
                    or not isinstance(unit.get("checks"), list) or not unit["checks"]
                    or not all(supported(check) for check in unit["checks"])):
                raise ValueError("Each unit needs its ID, firmware (or none), and passing observations with cited evidence")
            firmware = contract.get("firmware")
            if firmware:
                image = (root / firmware).resolve()
                if not image.is_relative_to(root) or not image.is_file() or unit["firmware"] != file_sha256(image):
                    raise ValueError(f"First article needs the tested firmware SHA-256 for {unit['id']}")
            for name in (spec or {}).get("requires_decisions", []):
                expected = file_sha256(root / name)
                if not any(check.get("decision") == name and check.get("decision_sha256") == expected
                           for check in unit["checks"]):
                    raise ValueError(f"First article needs the assembly test for {name} on unit {unit['id']}")
# The names of actions a card may name that have no script yet: none in this pack.
LATER = ()
# A card's tool that the one on it runs once its inputs are written: next starts the card and names the command,
# and the tool records the outcome itself.
def routing_command(board, profile):
    def quote(value):
        return "'" + value.replace("'", "''") + "'" if os.name == "nt" else shlex.quote(value)

    if tool_paths.is_external():
        command = ("& " if os.name == "nt" else "") + quote(sys.executable) + " -B " + quote(
            str(TOOL_ROOT / "scripts/routing_run.py")) + " --project " + quote(str(tool_paths.project_root()))
    else:
        command = ("& " if os.name == "nt" else "") + quote(sys.executable) + " -B scripts/routing_run.py"
    command += board.argument + " " + quote(profile.get("tasks", "TASKS")) + " --by <who> --attempt <id>"
    for field in ("provider", "model", "review_provider", "review_model", "effort"):
        option = field.replace("_", "-")
        value = profile.get(field)
        if value or field == "provider":
            command += " --" + option + " " + quote(value or "<provider>")
    return command


COMMANDS = {"routing_run": routing_command(boards.chosen(), DESCRIPTION.routing_agent)}


def copper_origin(root):
    """The Routing card's gate: a line for each batch of copper the branch added outside the routing run and still
    holds (scripts/board_copper_origin.py); none when every track, via and pour is accounted for."""
    root = pathlib.Path(root)
    try:
        return board_copper_origin.lines(board_copper_origin.unaccounted(
            root, BOARD, root / boards.chosen(root).records, strict=True))
    except (board_copper_origin.Unreadable, boards.BoardsError, ValueError) as error:
        return [f"where the board's copper came from could not be read: {error}"]


# What a card must show before it records pass or more_work, by its action (scripts/graph_pack.py, gates).
GATES = {"routing_run": Gate(copper_origin, board_copper_origin.WAY_OUT, board_copper_origin.WARNING)}



def render_models(root, read=None):
    board = root / BOARD
    content = read(BOARD) if read else board.read_bytes()
    models, _ = board_review_inputs.model_inputs(content.decode("utf-8"), board.parent, root, git_read=read)
    missing = [name for name, value in models.items() if not value]
    if missing:
        raise ValueError("Cannot verify rendered model files: " + ", ".join(missing))
    return {"models": models}


def routing_constraints(root, read=None):
    read = read or (lambda name: (root / name).read_bytes())
    try:
        rules = sha256(read(RULES))
    except FileNotFoundError:
        rules = "absent"  # KiCad allows projects without custom rules; later addition changes the scope.
    settings = board_review_inputs.routing_project(json.loads(read(PROJECT_FILE)))
    return {RULES: rules, PROJECT_FILE: sha256(json.dumps(settings, sort_keys=True).encode("utf-8"))}


def sealed_package(root):
    report = package(root)
    if report["findings"]:
        raise ValueError("; ".join(report["findings"][:3]))
    archive = report["detail"]["complete_package"]
    manifest = pathlib.Path(root) / PRODUCTION / "package-manifest.json"
    return {"archive": archive["file"], "sha256": archive["sha256"], "manifest_sha256": file_sha256(manifest)}


def pack():
    """The electronics pack (scripts/graph_pack.py): this file's card scripts and views, and the folders the
    board's description names. The engine adds the project's own card scripts to `ACTIONS` when it loads the pack."""
    written = (REPORTS, DESCRIPTION.rel("production"), pathlib.PurePosixPath(BOM).parent.as_posix(),
               "tools/editor_step/results")
    folders = dict.fromkeys((DESCRIPTION.rel("folder"), DATASHEETS, *MODEL_DIRS))
    return Pack("electronics", REPORTS, actions=ACTIONS, later=LATER, commands=COMMANDS, views=VIEWS,
                view_file=BOARD, branch_folders=folders, written=written, module="scripts/board_graph_actions.py",
                gates=GATES, legacy_release={("verification", None): "independent_review",
                    ("verification", "freshness"): "independent_review", ("product_judgment", "*"): "first_article"},
                legacy_pass={("verification", "freshness"): "freshness", ("package", "package"): "package"},
                release_checks={"independent_review": lambda spec, evidence, root: check_shipping_pass(
                                    "verification", None, evidence, root, spec),
                                "first_article": lambda spec, evidence, root: check_shipping_pass(
                                    "product_judgment", None, evidence, root, spec)},
                approval_sources={"sealed_package": sealed_package, "render_models": render_models,
                                             "routing_constraints": routing_constraints})
