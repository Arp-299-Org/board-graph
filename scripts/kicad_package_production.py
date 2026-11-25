#!/usr/bin/env python3
"""Create a self-auditing KiCad production package and checksum for the board its description names
(scripts/board_description.py; `--board-description`).

The gate reports it reads are the board checks' own, as scripts/validate_board.py writes them into the
Package card's folder (`--reports`) and scripts/kicad_report_freshness.py stamps them there; nothing is
copied or renamed by hand. The
G2 reference list of accepted library warnings stays where the description's drc_exclusions puts it, with its own
stamp (`--exclusions`). A board whose description names no electrical audit, or no full board audit, is packaged
without that gate, and the manifest says so; without a full board audit the track, via and pad counts come from the
saved board's text and the parts left unpopulated from the assembly manifest.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import importlib.util
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

CLI_WRAPPER = pathlib.Path(__file__).resolve().parents[1] / 'tools/kicad/invoke_kicad_cli.py'
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import board_description  # noqa: E402
import routing_evidence  # noqa: E402
from kicad_generate_fabrication_archive import board_copper, board_extra_layers, fabrication_layers  # noqa: E402
from kicad_sexp import children, parse  # noqa: E402

# The board checks' reports, by the names scripts/validate_board.py writes (its gates G1 to G6); drc.json carries
# the schematic parity and the unconnected items, exhaustive-board-audit.json names its footprint records.
CHECK_REPORTS = {
    'erc': 'erc.json',
    'drc': 'drc.json',
    'route': 'route-quality.json',
    'electrical': 'electrical-audit.json',
    'mask': 'pad-mask.json',
    'exhaustive': 'exhaustive-board-audit.json',
}
DESCRIPTION = board_description.load()
EXCLUSIONS_DEFAULT = DESCRIPTION.path('drc_exclusions')
VISUAL_INSPECTION = 'VISUAL_INSPECTION.md'
CRITIC_REPORT = 'G10_BOARD_CRITIC_REPORT.md'


def run_cli(*arguments) -> None:
    result = subprocess.run([sys.executable, '-X', 'utf8', str(CLI_WRAPPER), *map(str, arguments)],
                            capture_output=True, text=True, creationflags=0x08000000 if sys.platform == 'win32' else 0)
    if result.returncode:
        raise RuntimeError(f'Isolated KiCad export failed: {result.stderr}\n{result.stdout}')


def normalized_fabrication(name: str, data: bytes) -> object:
    if name.endswith('.gbrjob'):
        job = json.loads(data)
        job.get('Header', {}).pop('CreationDate', None)
        return job
    return [re.sub(r'^(; DRILL file KiCad .+? date ).*$', r'\1<creation timestamp>', line)
            for line in data.decode('utf-8').splitlines()
            if not (line.startswith('%TF.CreationDate,') or line.startswith('G04 Created by KiCad')
                    or line.startswith('; #@! TF.CreationDate,'))]


def verify_fabrication_derives_from_board(archive_path: pathlib.Path, board: pathlib.Path) -> dict:
    """Compare every delivered layer with a fresh export, including all paste regions. The copper layers exported
    are the board's own, read from its saved file, the same way the archive's generator reads them."""
    layers = fabrication_layers(board_copper(board), board_extra_layers(board))
    with tempfile.TemporaryDirectory(prefix='board-graph-package-fab-') as folder:
        root = pathlib.Path(folder)
        project = root / board.stem
        project.mkdir()
        copy = project / board.name
        for suffix in ('.kicad_pcb', '.kicad_pro', '.kicad_dru'):
            source = board.with_suffix(suffix)
            if not source.is_file():
                raise RuntimeError(f'Missing source companion: {source}')
            shutil.copy2(source, copy.with_suffix(suffix))
        gerbers = root / 'gerbers'
        gerbers.mkdir()
        run_cli('pcb', 'export', 'gerbers', '--output', str(gerbers) + '/',
                '--layers', ','.join(layers), '--subtract-soldermask', copy)
        run_cli('pcb', 'export', 'drill', '--output', str(gerbers) + '/', '--format', 'excellon',
                '--drill-origin', 'absolute', '--excellon-separate-th', '--generate-map', '--map-format', 'gerberx2', copy)
        fresh = {p.name: p.read_bytes() for p in gerbers.iterdir() if p.is_file()}
        with zipfile.ZipFile(archive_path) as archive:
            names = [i.filename for i in archive.infolist() if not i.is_dir()]
            if len(names) != len(set(names)) or set(names) != set(fresh):
                raise RuntimeError('Fabrication archive member set differs from the fresh board export')
            for name in names:
                if normalized_fabrication(name, archive.read(name)) != normalized_fabrication(name, fresh[name]):
                    raise RuntimeError(f'Fabrication content differs from the board: {name}')
        return {'all_layers_match_fresh_export': True, 'files_checked': sorted(fresh), 'layers_exported': layers,
                'normalization': 'Creation timestamps only; paste flashes and every polygon region compared.'}


def documented_drc_warnings(drc: dict, documented: dict) -> list[dict]:
    # main() requires this document to be bound to the packaged revision before
    # reaching here. The reviewed document owns the allowance, not a second,
    # frozen list of references from an older amplifier circuit.
    allowed = documented.get('accepted_warning_refs')
    if (not isinstance(allowed, list)
            or any(not isinstance(ref, str) or not re.fullmatch(r'[A-Za-z]+\d+', ref) for ref in allowed)
            or len(set(allowed)) != len(allowed)):
        raise RuntimeError('Documented DRC allowance needs unique part references')
    expected = set(allowed)
    found = []
    for violation in drc['violations']:
        refs = re.findall(r'Footprint\s+([^\s:]+)', ' '.join(i.get('description', '') for i in violation.get('items', [])))
        if (violation.get('type') != 'lib_footprint_mismatch' or violation.get('severity') != 'warning'
                or len(refs) != 1 or refs[0] not in expected):
            raise RuntimeError(f'Unaccepted DRC violation: {violation}')
        found.append({'reference': refs[0], 'excluded': bool(violation.get('excluded', False))})
    if {v['reference'] for v in found} != expected or len(found) != len(expected):
        raise RuntimeError('DRC warning set changed; review the new report before packaging')
    return found


def pad_mask_complete(mask: dict, pad_count: int) -> bool:
    """The mask report found no covered pad and looked at every pad the board has, apart from those it says it
    skipped (a net tie's, copper joining two nets under mask by design)."""
    skipped = mask.get('pads_skipped') or {}
    return (mask.get('findings') == [] and isinstance(mask.get('pads_checked'), int)
            and mask['pads_checked'] + sum(skipped.values()) == pad_count)


def gate_reports(description) -> dict:
    """The board checks' reports a package reads: every one in CHECK_REPORTS, less the electrical audit's when the
    description names none and the full board audit's when it names none."""
    return {key: name for key, name in CHECK_REPORTS.items()
            if (key != 'electrical' or description.electrical_audit)
            and (key != 'exhaustive' or description.board_audit)}


def saved_board_counts(text: str) -> dict:
    """The tracks (straight segments, as the full board audit counts them), vias and footprint pads in a saved
    board's text: the counts a board without its own full board audit is packaged with."""
    board = parse(text)
    return {'tracks': len(children(board, 'segment')), 'vias': len(children(board, 'via')),
            'pads': sum(len(children(footprint, 'pad')) for footprint in children(board, 'footprint'))}


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def recursive_list_count(value: object, key: str) -> int:
    if isinstance(value, dict):
        return sum(
            (len(item) if name == key and isinstance(item, list) else recursive_list_count(item, key))
            for name, item in value.items()
        )
    if isinstance(value, list):
        return sum(recursive_list_count(item, key) for item in value)
    return 0


def normalized_ipc(path: pathlib.Path) -> list[str]:
    """IPC-D-356 lines with KiCad's per-export ID removed.

    Every via line carries a trailing S<digits> field that changes on each
    export even when the board has not moved, so a raw byte comparison would
    always disagree. Strip only that field; everything else, including every pad
    and via coordinate, still has to match.
    """
    return [
        # The ID is signed: KiCad emitted S366158371 on one export and
        # S-955849581 on the next. A digits-only pattern here made this gate
        # reject a correct bundle about half the time, which is the exact way a
        # noisy check gets switched off and stops protecting anything.
        re.sub(r"S-?\d+\s*$", "S<id>", line.rstrip())
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def verify_netlist_derives_from_board(netlist: pathlib.Path, board: pathlib.Path) -> None:
    """Re-export the test netlist from the board and require the same content.

    This is the strong form of the staleness check, and it is here because
    netlist.ipc is what went stale: it was five weeks older than the board while
    the README called it fresh. The mtime gate below would also have caught that,
    but mtimes are a property of this working copy rather than of the file, and a
    fresh git checkout rewrites them all to the checkout time. Content cannot be
    rewritten by a checkout, so compare content.
    """
    if not CLI_WRAPPER.is_file():
        raise RuntimeError(
            f"Isolated KiCad launcher not found at {CLI_WRAPPER}, so netlist.ipc cannot be verified "
            f"against the board. Refusing to package on the weaker mtime evidence alone."
        )
    with tempfile.TemporaryDirectory() as work:
        fresh = pathlib.Path(work) / "netlist.ipc"
        run_cli('pcb', 'export', 'ipcd356', '--output', fresh, board)
        if not fresh.is_file():
            raise RuntimeError(
                f"Could not re-export IPC-D-356 from {board.name} to verify "
                'netlist.ipc: the export did not create its output.'
            )
        have, want = normalized_ipc(netlist), normalized_ipc(fresh)
    if have == want:
        return
    detail = f"{len(have)} lines bundled versus {len(want)} freshly exported"
    for line_no, (a, b) in enumerate(zip(have, want), start=1):
        if a != b:
            detail = f"first difference at line {line_no}:\n    bundled: {a}\n    board:   {b}"
            break
    raise RuntimeError(
        f"{netlist.name} does not match a fresh export from {board.name}, so it "
        f"describes a different layout:\n  {detail}\n"
        f"Regenerate it: kicad-cli pcb export ipcd356 --output {netlist} {board}"
    )


def revision_name(package_name: str, tail: str) -> re.Pattern:
    """A dated revision name of this board's packages: the description's package_name (a regular expression),
    "_<yyyy-mm-dd>", then `tail`, ignoring case."""
    return re.compile(package_name + r"_\d{4}-\d{2}-\d{2}" + tail, re.I)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--production", type=pathlib.Path, required=True)
    parser.add_argument("--reports", type=pathlib.Path, required=True)
    parser.add_argument("--base-name", required=True)
    parser.add_argument('--check-only', action='store_true',
                        help='Check G1-G9 without sealing a package or certifying G10/G11.')
    parser.add_argument(
        "--gerber-archive-name",
        help="Gerber/drill ZIP filename when it differs from BASE-NAME.zip",
    )
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--checksum-output", type=pathlib.Path, required=True)
    parser.add_argument("--exclusions", type=pathlib.Path,
                        help="the G2 reference list of accepted library warnings, stamped in its own folder; "
                             "the description's drc_exclusions by default")
    board_description.add_argument(parser)
    parser.add_argument(
        "--generated-date",
        default=datetime.date.today().isoformat(),
        help="ISO date recorded in package-manifest.json (defaults to today)",
    )
    args = parser.parse_args()
    description = board_description.load(args.board_description) if args.board_description else DESCRIPTION
    electrical_audit = description.electrical_audit
    board_audit = description.board_audit
    package_name = description['package_name']

    production = args.production.resolve()
    reports = args.reports.resolve()
    exclusions = (args.exclusions or description.path('drc_exclusions')).resolve()
    output = args.output.resolve()
    checksum_output = args.checksum_output.resolve()

    check_reports = gate_reports(description)
    GATE_REPORTS = list(check_reports.values())

    # These reports support the engineering gates; G10 and G11 are separate.
    # Consuming a report generated against a different board silently certifies
    # the wrong design, so prove they describe the board being packaged first.
    freshness_spec = importlib.util.spec_from_file_location(
        "kicad_report_freshness",
        pathlib.Path(__file__).resolve().parent / "kicad_report_freshness.py",
    )
    freshness = importlib.util.module_from_spec(freshness_spec)
    sys.modules["kicad_report_freshness"] = freshness
    freshness_spec.loader.exec_module(freshness)
    assembly_board = pathlib.Path(
        read_json(production / "assembly-manifest.json")["board"]
    )
    if not assembly_board.is_absolute():
        assembly_board = (production / assembly_board).resolve()
    freshness.require_fresh(reports, assembly_board, GATE_REPORTS)
    freshness.require_fresh(exclusions.parent, assembly_board, [exclusions.name])

    erc = read_json(reports / CHECK_REPORTS['erc'])
    drc = read_json(reports / CHECK_REPORTS['drc'])
    route_report = read_json(reports / CHECK_REPORTS['route'])
    route = route_report["summary"]
    accepted_route_warnings: list[dict] = []
    unaccepted_route_count = 0
    if route.get("warnings"):
        pcb_rel = description.rel("pcb")
        search_dirs = routing_evidence.routing_receipt_dirs(description.root, pcb_rel, description.routing_agent)
        accepted_map = routing_evidence.accepted_route_quality_warnings(description.root, pcb_rel, search_dirs)
        accepted_route_warnings, unaccepted_route_items, unlisted_route = routing_evidence.classify_route_quality_warnings(
            route_report, accepted_map
        )
        unaccepted_route_count = len(unaccepted_route_items) + unlisted_route
    elif route.get("warnings") != 0:
        unaccepted_route_count = 1
    exhaustive = read_json(reports / CHECK_REPORTS['exhaustive']) if board_audit else None
    assembly = read_json(production / "assembly-manifest.json")
    counts = (exhaustive["counts"] if board_audit
              else saved_board_counts(assembly_board.read_text(encoding="utf-8")))
    electrical = read_json(reports / CHECK_REPORTS['electrical']) if electrical_audit else None
    mask = read_json(reports / CHECK_REPORTS['mask'])
    accepted_warnings = documented_drc_warnings(drc, read_json(exclusions))
    waiver_path = description.root / electrical_audit['waivers'] if electrical_audit else None
    waiver_hash = sha256(waiver_path).upper() if electrical_audit else None

    erc_violations = recursive_list_count(erc, "violations")
    drc_violations = 0  # all reported warnings matched above; anything else raises
    parity_issues = len(drc['schematic_parity'])
    unconnected_items = len(drc["unconnected_items"])
    gate_failures: list[str] = []
    if electrical_audit:
        if electrical.get('pass') is not True or any(electrical.get('gated_failures', {}).values()):
            gate_failures.append('Electrical audit has a failed gate')
        if electrical.get('rules', {}).get('R11', {}).get('loop_estimate', {}).get('complete') is not True:
            gate_failures.append('Electrical R11 loop-area report is incomplete')
        if electrical.get('waiver_sha256') != waiver_hash:
            gate_failures.append('Electrical audit does not bind the current signed waiver file')
    if not pad_mask_complete(mask, counts['pads']):
        gate_failures.append('Pad-mask check is incomplete or contains covered pads')
    if assembly.get('board_sha256') != sha256(assembly_board).upper():
        gate_failures.append('Assembly validation describes a different board')
    required_bound_files = {f'{args.base_name}{suffix}' for suffix in
                            ('_bom.csv', '_positions.csv', '_SMT_bom.csv', '_SMT_positions.csv')}
    required_bound_files.add(args.gerber_archive_name or f'{args.base_name}.zip')
    bound_files = assembly.get('validated_file_sha256', {})
    if set(bound_files) != required_bound_files:
        gate_failures.append('Assembly validation does not bind the delivered BOM/CPL/archive files')
    for name, digest in bound_files.items():
        path = production / name
        if not path.is_file() or sha256(path).upper() != digest:
            gate_failures.append(f'File changed after assembly validation: {name}')
    if erc_violations:
        gate_failures.append(f"ERC violations: {erc_violations}")
    if drc_violations:
        gate_failures.append(f"DRC violations: {drc_violations}")
    if parity_issues:
        gate_failures.append(f"schematic parity issues: {parity_issues}")
    if unconnected_items:
        gate_failures.append(f"unconnected items: {unconnected_items}")
    if route["errors"] or unaccepted_route_count:
        detail = f"route quality: {route['errors']} errors, {route['warnings']} warnings"
        if accepted_route_warnings:
            detail += f" ({unaccepted_route_count} unreviewed, {len(accepted_route_warnings)} reviewer-accepted)"
        gate_failures.append(detail)
    if board_audit and exhaustive["status"] != "pass":
        gate_failures.append(f"exhaustive board audit: {exhaustive['status']}")
    if not {"error", "warning"}.issubset(set(erc.get("included_severities", []))):
        gate_failures.append("ERC report does not include both errors and warnings")
    if not {"error", "warning"}.issubset(set(drc.get("included_severities", []))):
        gate_failures.append("DRC report does not include both errors and warnings")

    required_assembly_flags = [
        "all_smt_parts_have_lcsc_numbers",
        "bom_cpl_reference_sets_match",
        "placement_coordinates_match_board_absolute_origin",
        "fabrication_archive_required_layers_present",
    ]
    for name in required_assembly_flags:
        if assembly.get(name) is not True:
            gate_failures.append(f"assembly validation is not true: {name}")
    paste_counts = assembly.get("paste_flash_counts", {})
    visual_evidence = reports / VISUAL_INSPECTION
    critic_evidence = reports / CRITIC_REPORT
    if not args.check_only and not visual_evidence.is_file():
        gate_failures.append(f"missing visual inspection evidence: {visual_evidence}")
    if not args.check_only and (not critic_evidence.is_file() or
            not re.match(r'^PASS(?:\s*[—:\-]|\s*$)', critic_evidence.read_text(encoding='utf-8').splitlines()[0])):
        gate_failures.append('Missing G10 PASS for this exact package')
    if gate_failures:
        raise RuntimeError("Production package gates failed:\n- " + "\n- ".join(gate_failures))

    board_path = assembly_board
    schematic_path = board_path.with_suffix(".kicad_sch")
    if not board_path.is_file() or not schematic_path.is_file():
        raise RuntimeError(
            f"Authoritative design sources missing: {board_path}, {schematic_path}"
        )

    # A bundle must contain exactly one revision. Shipping several is how an
    # operator ends up uploading the wrong Gerbers, and the README pointing at an
    # obsolete archive is the same failure wearing a different hat. Fail loudly
    # rather than quietly excluding, so the stale files get disposed deliberately.
    foreign = sorted(
        path.relative_to(production).as_posix()
        for path in production.rglob("*")
        if path.is_file()
        and "superseded" not in path.relative_to(production).parts
        and revision_name(package_name, r"(?:_|\.)").match(path.name)
        and not path.name.startswith(args.base_name)
    )
    if foreign:
        raise RuntimeError(
            f"Production directory holds files from another revision, so this bundle "
            f"would ship more than one:\n  " + "\n  ".join(foreign) +
            f"\nDispose of them before packaging {args.base_name}.")

    # The one bundle file that can be checked by content is checked by content.
    verify_netlist_derives_from_board(production / "netlist.ipc", board_path)
    fabrication_check = verify_fabrication_derives_from_board(
        production / (args.gerber_archive_name or f'{args.base_name}.zip'), board_path)

    # Nothing else in a release bundle may predate the board it describes.
    #
    # Read the limits of this check before trusting it: mtimes belong to this
    # working copy, not to the files. A fresh git clone or a branch switch stamps
    # every file with the checkout time, which makes this gate silently pass on a
    # machine that has never run any of the generators. It is a real guard against
    # the failure that happened here, where a five-week-old netlist sat in a
    # bundle the README called fresh, and it is not a proof of derivation. The
    # only proof is regeneration and comparison, as above. Reports get the
    # stronger treatment separately, through require_fresh's SHA-256 provenance,
    # which is content-based and survives a checkout.
    board_mtime = board_path.stat().st_mtime
    older = sorted(
        f"{path.relative_to(production).as_posix()} "
        f"({datetime.datetime.fromtimestamp(path.stat().st_mtime):%Y-%m-%d %H:%M})"
        for path in production.rglob("*")
        if path.is_file()
        and "superseded" not in path.relative_to(production).parts
        and path.name != "package-manifest.json"
        and path.stat().st_mtime < board_mtime - 1
    )
    if older:
        raise RuntimeError(
            f"These bundle files predate the board "
            f"({datetime.datetime.fromtimestamp(board_mtime):%Y-%m-%d %H:%M}), so they "
            f"cannot describe it:\n  " + "\n  ".join(older) +
            "\nRegenerate them.")

    # Revision rot in prose: the README told operators to upload a July 12 archive
    # that was not even in the bundle. Any date-stamped revision name in a text
    # file has to be this one.
    rot: list[str] = []
    for path in sorted(production.rglob("*")):
        if (not path.is_file() or path.suffix.lower() not in {".md", ".txt", ".json"}
                or "superseded" in path.relative_to(production).parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for found in sorted(set(revision_name(package_name, r"(?:_[A-Z0-9_]+)?").findall(text))):
            if not found.startswith(args.base_name):
                rot.append(f"{path.relative_to(production).as_posix()} names {found}")
    if rot:
        raise RuntimeError(
            "Bundle text refers to another revision:\n  " + "\n  ".join(rot) +
            f"\nUpdate it for {args.base_name}.")

    if args.check_only:
        print(json.dumps({'engineering_package_checks': 'pass', 'board_sha256': sha256(board_path).upper(),
                          'schematic_sha256': sha256(schematic_path).upper(),
                          'validated_file_sha256': bound_files,
                          'accepted_library_warnings': accepted_warnings,
                          'accepted_route_quality_warnings': accepted_route_warnings,
                          'signed_waiver_sha256': waiver_hash,
                          'fabrication': fabrication_check, 'sealed': False,
                          'G10_G11': 'Separate critic PASS and user supplier-preview acceptance required.'}, indent=2))
        return

    validation_dir = production / "validation"
    validation_dir.mkdir(parents=True, exist_ok=True)
    validation_evidence = [*check_reports.values(), VISUAL_INSPECTION, CRITIC_REPORT]
    # Everything shipped as validation has to describe the board being packaged.
    # VISUAL_INSPECTION.md used to travel in here unchecked while describing a
    # 497-track board against this one's 563.
    freshness.require_fresh(reports, assembly_board, validation_evidence)
    for name in validation_evidence:
        source = reports / name
        if not source.is_file():
            raise RuntimeError(f"Missing validation evidence: {source}")
        shutil.copy2(source, validation_dir / name)
    shutil.copy2(exclusions, validation_dir / exclusions.name)
    if electrical_audit:
        shutil.copy2(waiver_path, validation_dir / waiver_path.name)
    # One provenance for the validation folder: the checks' stamps and the exclusion list's own.
    provenance = read_json(reports / 'provenance.json')
    provenance[exclusions.name] = read_json(exclusions.parent / 'provenance.json')[exclusions.name]
    (validation_dir / 'provenance.json').write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n",
                                                    encoding='utf-8')
    # The footprint records the full board audit wrote beside its report, in the same run and against the same
    # board, so that report's stamp vouches for them.
    if board_audit:
        footprints = reports / exhaustive['footprints']['file']
        if not footprints.is_file():
            raise RuntimeError(f"Missing validation evidence: {footprints}")
        shutil.copy2(footprints, validation_dir / footprints.name)

    gerber_archive = production / (
        args.gerber_archive_name or f"{args.base_name}.zip"
    )
    with zipfile.ZipFile(gerber_archive) as archive:
        gerber_entries = [
            {"path": info.filename, "bytes": info.file_size}
            for info in sorted(archive.infolist(), key=lambda item: item.filename)
            if not info.is_dir()
        ]

    manifest_path = production / "package-manifest.json"
    files = [
        path
        for path in sorted(production.rglob("*"))
        if path.is_file() and path != manifest_path
        and "superseded" not in path.relative_to(production).parts
    ]
    manifest = {
        'project': f"{description['project']} {board_path.stem}",
        "generated": args.generated_date,
        "kicad_version": drc["kicad_version"],
        "design_sources": {
            "schematic": {
                "path": schematic_path.name,
                "sha256": sha256(schematic_path),
            },
            "pcb": {
                "path": board_path.name,
                "sha256": sha256(board_path),
            },
            'signed_waivers': ({'path': f'validation/{waiver_path.name}', 'sha256': waiver_hash} if electrical_audit
                               else 'none: the description names no electrical audit'),
        },
        "gates": {
            "erc_violations": erc_violations,
            "active_drc_violations": drc_violations,
            'accepted_library_warnings': accepted_warnings,
            'electrical_audit': electrical['pass'] if electrical_audit else 'none for this board',
            **({'R11_loop_area_report_complete': electrical['rules']['R11']['loop_estimate']['complete']}
               if electrical_audit else {}),
            'pad_mask_findings': len(mask['findings']),
            'fabrication_matches_fresh_export': fabrication_check,
            "schematic_parity_issues": parity_issues,
            "unconnected_items": unconnected_items,
            "route_quality_errors": route["errors"],
            "route_quality_warnings": route["warnings"],
            "accepted_route_quality_warnings": accepted_route_warnings,
            "exhaustive_board_audit": exhaustive["status"] if board_audit else 'none for this board',
            "visual_inspection": "pass",
            "visual_inspection_evidence": {
                "path": f"validation/{visual_evidence.name}",
                "sha256": sha256(visual_evidence),
            },
            "tracks": counts["tracks"],
            "vias": counts["vias"],
            "pads": counts["pads"],
        },
        "assembly": {
            "smt_components": assembly["smt_component_count"],
            "manual_through_hole_components": assembly["manual_through_hole_references"],
            'production_dnp': exhaustive['footprints']['dnp'] if board_audit else assembly['production_dnp'],
            "smt_missing_lcsc_numbers": 0,
            "placement_origin": "absolute; matches Gerbers",
            "placement_rotation_convention": assembly["placement_rotation_convention"],
            "paste_flash_counts": paste_counts,
            'paste_region_counts': assembly['paste_region_counts'],
        },
        "gerber_archive_entries": gerber_entries,
        "files": [
            {
                "path": path.relative_to(production).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": sha256(path),
            }
            for path in files
        ],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    package_files = sorted(
        path for path in production.rglob("*")
        if path.is_file() and "superseded" not in path.relative_to(production).parts
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in package_files:
            archive.write(path, path.relative_to(production).as_posix())

    checksum = sha256(output).upper()
    checksum_output.write_text(f"{checksum}  {output.name}\n", encoding="ascii")

    # The bundle lands one directory above `production/`, and until now no gate
    # looked there at all. Two failures were sitting in it: an older revision's
    # complete-package zip beside the current one, and two checksum files naming
    # the same zip with different hashes, because a previous run used a slightly
    # different --checksum-output filename. A second checksum file asserting an
    # old hash is worse than none: an operator who verifies the download and sees
    # a mismatch has been told, by this repo, that a good file is corrupt.
    landing = output.parent
    contradictions = sorted(
        f"{path.name} claims {path.read_text(encoding='ascii').split()[0]}"
        for path in landing.glob("*.sha256")
        if path != checksum_output
        and path.is_file()
        and output.name in path.read_text(encoding="ascii", errors="replace")
    )
    if contradictions:
        raise RuntimeError(
            f"Another checksum file already describes {output.name} with a "
            f"different hash ({checksum} is correct):\n  " +
            "\n  ".join(contradictions) +
            "\nDelete the stale one; two answers to 'is my download intact' is "
            "worse than none.")
    strays = sorted(
        path.name for path in landing.iterdir()
        if path.is_file()
        and revision_name(package_name, r".*-complete-package\.").match(path.name)
        and not path.name.startswith(args.base_name)
    )
    if strays:
        raise RuntimeError(
            f"Older complete packages sit beside {output.name}, so an operator "
            f"can pick up the wrong revision:\n  " + "\n  ".join(strays) +
            f"\nMove them to {landing.name}/superseded/<date>/ before packaging.")
    print(
        json.dumps(
            {
                "output": str(output),
                "bytes": output.stat().st_size,
                "files": len(package_files),
                "sha256": checksum,
                "tracks": counts["tracks"],
                "vias": counts["vias"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
