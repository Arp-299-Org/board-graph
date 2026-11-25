#!/usr/bin/env python3
"""Board checks for a pull request or for main: the order package's gates, run fresh.

Runs ERC, DRC with schematic parity, the saved-fill check, route quality, where the
change's copper came from (with --base), the electrical audit, pad mask, thermal relief, the hand-soldered joints, the exhaustive audit and routing progress against the
committed board its description names (scripts/board_description.py), and writes each tool's report
plus summary.json and summary.md to --out. Each report is removed before its tool
runs, so every report in --out is from this run. Nothing tracked in the repository
is written: DRC refills zones in memory (no --save-board), and the saved-fill check
refills a copy of the project under --out. kicad-cli does write the project's
local settings file (<project>.kicad_prl beside the board), which git ignores.

  python3 scripts/validate_board.py --out <dir> [--base <ref>] [--board-description <path>]

With --base, a change that touches nothing the board checks read passes at once,
and a change that does is also checked for added electrical waivers. A check that
fails exactly as it does on the base commit is reported as inherited and does not
fail the change: a pull request answers for what it breaks, and main's own run
stays red until the failure is fixed. Exit 1 if any other check fails.

A board is unfinished for most of its life, so while anything is still unconnected
these do not fail: unconnected items, pads with nothing on them, and electrical audit
findings, which are listed by name. Main can carry a board in progress and stay green.
A change fails if it raises the open count without moving a part. Ordering is gated
separately by scripts/kicad_package_production.py, which accepts no unconnected item
and no failed electrical audit.

A board whose description names no electrical audit or no full board audit gets that
row as a pass that says it was skipped.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

_SCRIPTS = pathlib.Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
import tool_paths  # noqa: E402

if __name__ == "__main__":
    tool_paths.take_argument()

TOOL_ROOT = tool_paths.TOOL_ROOT
ROOT = tool_paths.project_root()
sys.path.insert(0, str(TOOL_ROOT / "tools" / "kicad"))
from invoke_kicad_cli import run_kicad_cli  # noqa: E402

import board_copper_origin  # noqa: E402
import board_description  # noqa: E402
import boards  # noqa: E402
import routing_evidence  # noqa: E402

# What the checks read besides the board's own files. kicad_preflight.ps1 loads invoke_console.ps1.
GENERAL_INPUTS = ("scripts/kicad", "scripts/invoke_console.ps1", "scripts/validate_board.py",
                  "scripts/board_manual_joints.py", "scripts/board_description.py", "scripts/board_copper_origin.py",
                  "scripts/routing_evidence.py", "tools/kicad/", ".github/workflows/validate.yml")


def use(description: board_description.Description) -> None:
    """Point every check at the board a description names. Called once at import with the default description
    (or BOARD_GRAPH_BOARD's), and again by main() when --board-description names another."""
    global DESCRIPTION, PROJECT, BOARD, SCHEMATIC, DOCUMENTED_DRC, ELECTRICAL, WAIVERS, BOARD_INPUTS
    DESCRIPTION = description
    PROJECT = description.project_folder
    BOARD = description.path("pcb")
    SCHEMATIC = description.path("schematic")
    DOCUMENTED_DRC = description.path("drc_exclusions")
    ELECTRICAL = description.electrical_audit
    WAIVERS = ELECTRICAL["waivers"] if ELECTRICAL else None
    # A change to any of these can change what the board checks report.
    own = [description.rel("folder").rstrip("/") + "/", description.rel("parts_data"),
           *([description.board_audit["script"]] if description.board_audit else []),
           *([ELECTRICAL["script"], WAIVERS] if ELECTRICAL else [])]
    try:
        own.append(description.file.resolve().relative_to(ROOT).as_posix())
    except ValueError:
        pass  # a description outside the repository is in no diff of it
    BOARD_INPUTS = tuple(dict.fromkeys((*own, *GENERAL_INPUTS)))


use(board_description.load())
# What a zone refill reads from the project folder: the board, its rules, and its libraries.
REFILL_INPUTS = (".kicad_pcb", ".kicad_pro", ".kicad_dru", "-lib-table", ".pretty", ".kicad_sym")


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    """A console child that never opens a window on Windows (see invoke_kicad_cli.py)."""
    options: dict = {}
    if sys.platform == "win32":
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        options.update(startupinfo=startup, creationflags=subprocess.CREATE_NO_WINDOW,
                       stdin=subprocess.DEVNULL)
    env = None
    if tool_paths.is_external():
        env = dict(os.environ)
        env[tool_paths.ENV] = str(ROOT)
    return subprocess.run(command, cwd=str(ROOT), capture_output=True, text=True, check=False, env=env, **options)


def kicad_offline(script: str, *script_args: object) -> subprocess.CompletedProcess[str]:
    """A registered pcbnew script through kicad_run.ps1, so the preflight (KiCad version) runs first."""
    quoted = ", ".join("'" + str(arg).replace("'", "''") + "'" for arg in script_args)
    runner = str(TOOL_ROOT / "scripts" / "kicad_run.ps1").replace("'", "''")
    return run(["pwsh", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
                f"& '{runner}' -Mode Offline -Project {PROJECT} -Script '{script}' -ScriptArgs @({quoted})"])


def result(name: str, ok: bool, detail: str, report: pathlib.Path | None = None) -> dict:
    return {"check": name, "ok": ok, "detail": detail, "report": report.name if report else None}


def tool_failure(name: str, done: subprocess.CompletedProcess[str], report: pathlib.Path | None) -> dict:
    # PowerShell colours its errors; the summary is a Markdown table, so the colour codes go.
    plain = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", done.stdout + done.stderr)
    tail = " ".join(plain.strip().splitlines()[-3:])
    return result(name, False, f"could not run (exit {done.returncode}): {tail[:300]}", report)


def cleared(report: pathlib.Path) -> pathlib.Path:
    """The report's path with any earlier report removed, so only this run's tool can fill it."""
    report.unlink(missing_ok=True)
    return report


def load(report: pathlib.Path) -> dict | None:
    """The report this run's tool wrote, or None when there is none or it is not whole JSON."""
    try:
        return json.loads(report.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def unconnected_count(report: dict | None) -> int | None:
    """The measured count, or None if DRC did not provide a usable list."""
    items = report.get("unconnected_items") if isinstance(report, dict) else None
    return len(items) if isinstance(items, list) else None


def drc(out: pathlib.Path, name: str, *extra: str) -> tuple[subprocess.CompletedProcess[str], pathlib.Path]:
    report = cleared(out / name)
    done = run_kicad_cli(["pcb", "drc", *extra, "--format", "json", "--severity-all", "--schematic-parity",
                          "--output", report, BOARD], capture_output=True)
    return done, report


def check_erc(out: pathlib.Path) -> dict:
    name = "Schematic rules check, ERC (G1)"
    report = cleared(out / "erc.json")
    done = run_kicad_cli(["sch", "erc", "--format", "json", "--severity-all", "--output", report, SCHEMATIC],
                         capture_output=True)
    erc = load(report)
    if done.returncode != 0 or erc is None:
        return tool_failure(name, done, report)
    violations = [v for sheet in erc["sheets"] for v in sheet.get("violations", [])]
    kinds = collections.Counter(f"{v.get('severity')} {v.get('type')}" for v in violations)
    return result(name, not violations,
                  "0 errors, 0 warnings" if not violations else f"{len(violations)}: {dict(kinds)}", report)


def check_drc(out: pathlib.Path) -> list[dict]:
    """G2 on a refilled board, and the KiCad version it ran on."""
    name = "Board rules check, DRC (G2), zones refilled"
    done, report = drc(out, "drc.json", "--refill-zones")
    refilled = load(report)
    if done.returncode != 0 or not isinstance(refilled, dict):
        return [result("KiCad version", False, "DRC did not run"), tool_failure(name, done, report)]
    accepted = set(json.loads(DOCUMENTED_DRC.read_text())["accepted_warning_refs"])
    other = [v for v in refilled.get("violations", []) if v.get("type") != "lib_footprint_mismatch"]
    mismatch_refs = set()
    for violation in refilled.get("violations", []):
        if violation.get("type") == "lib_footprint_mismatch":
            for item in violation.get("items", []):
                match = re.match(r"Footprint (\S+)", item.get("description", ""))
                if match:
                    mismatch_refs.add(match.group(1))
    unconnected = unconnected_count(refilled)
    parity = len(refilled.get("schematic_parity", []))
    problems = []
    if unconnected is None:
        problems.append("DRC report has no usable unconnected-items list")
    if other:
        problems.append(f"{len(other)} violations: {dict(collections.Counter(v.get('type') for v in other))}")
    if mismatch_refs != accepted:
        new, gone = sorted(mismatch_refs - accepted), sorted(accepted - mismatch_refs)
        problems.append(f"library mismatches differ from the documented {len(accepted)} (new {new}, no longer {gone})")
    if parity:
        problems.append(f"{parity} schematic/board mismatches")
    library = f"only the {len(accepted)} documented library warnings" if accepted else "0 library warnings"
    g2 = result(name, not problems, "; ".join(problems) or f"0 copper violations, 0 mismatches, {library}", report)
    g2["open"] = unconnected
    version = refilled.get("kicad_version", "unknown")
    return [result("KiCad version", True, f"KiCad {version} (the preflight enforces the minimum)"), g2]


def closing(text: str, start: int) -> int:
    """The index just past the s-expression opening at or after start; quoted text may hold parentheses."""
    depth, quoted, at = 0, False, start
    while at < len(text):
        char = text[at]
        if quoted:
            if char == "\\":
                at += 1
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return at + 1
        at += 1
    raise ValueError("unbalanced s-expression")


def zone_fills(board: pathlib.Path) -> dict[str, tuple[str, list[str]]]:
    """Each board-level zone by its uuid: its layers, and its filled polygons with spacing made uniform."""
    text = board.read_text(encoding="utf-8")
    zones = {}
    for match in re.finditer(r"\n\t\(zone\s", text):
        zone = text[match.start() + 2:closing(text, match.start() + 2)]
        uuid = re.search(r'\(uuid "([^"]+)"\)', zone)
        layers = re.search(r"\n\t\t\(layers? ([^)]*)\)", zone)
        polygons, at = [], zone.find("(filled_polygon")
        while at >= 0:
            end = closing(zone, at)
            polygons.append(" ".join(zone[at:end].split()))
            at = zone.find("(filled_polygon", end)
        zones[uuid.group(1) if uuid else f"zone at {match.start()}"] = (layers.group(1) if layers else "?", polygons)
    return zones


def check_saved_fills(out: pathlib.Path) -> dict:
    """The saved board's zone fills against a copy refilled by kicad-cli: KiCad's refill is deterministic, so any
    difference means the board was saved without its fills current. Equal DRC results would not show it."""
    name = "Saved zone fills current"
    copy = out / "refilled" / PROJECT
    shutil.rmtree(out / "refilled", ignore_errors=True)
    copy.mkdir(parents=True)
    for item in (ROOT / PROJECT).iterdir():
        if item.name.endswith(REFILL_INPUTS):
            (shutil.copytree if item.is_dir() else shutil.copyfile)(item, copy / item.name)
    refilled = copy / BOARD.name
    report = cleared(out / "drc-refilled-copy.json")
    done = run_kicad_cli(["pcb", "drc", "--refill-zones", "--save-board", "--format", "json", "--output", report,
                          refilled], capture_output=True)
    if done.returncode != 0 or load(report) is None:
        return tool_failure(name, done, report)
    saved, fresh = zone_fills(BOARD), zone_fills(refilled)
    if saved.keys() != fresh.keys():
        return result(name, False, f"the refilled copy has {len(fresh)} zones, the board {len(saved)}", report)
    vertices = lambda polygons: sum(polygon.count("(xy ") for polygon in polygons)  # noqa: E731
    differ = [f"{layers}: {vertices(polygons)} saved vertices, {vertices(fresh[uuid][1])} refilled"
              for uuid, (layers, polygons) in saved.items() if polygons != fresh[uuid][1]]
    return result(name, not differ,
                  f"all {len(saved)} zones match a fresh refill" if not differ else
                  f"{len(differ)} of {len(saved)} zones differ from a fresh refill ({'; '.join(differ)}): "
                  "refill the zones in KiCad and commit the board", report)


def check_route_quality(out: pathlib.Path) -> dict:
    name = "Route quality (G3)"
    report = cleared(out / "route-quality.json")
    done = kicad_offline("scripts/kicad_route_quality.py", "--board", BOARD, "--output", report, "--no-fail")
    quality = load(report)
    if done.returncode != 0 or quality is None:
        return tool_failure(name, done, report)
    summary = quality["summary"]
    errors = summary.get("errors")
    warnings = summary.get("warnings")
    accepted_warnings: list[dict] = []
    unaccepted_count = 0
    if warnings:
        pcb_rel = DESCRIPTION.rel("pcb")
        search_dirs = routing_evidence.routing_receipt_dirs(ROOT, pcb_rel, DESCRIPTION.routing_agent)
        accepted_map = routing_evidence.accepted_route_quality_warnings(ROOT, pcb_rel, search_dirs)
        accepted_warnings, unaccepted_items, unlisted = routing_evidence.classify_route_quality_warnings(
            quality, accepted_map
        )
        unaccepted_count = len(unaccepted_items) + unlisted
    elif warnings != 0:
        unaccepted_count = 1
    ok = errors == 0 and unaccepted_count == 0
    note = ""
    if accepted_warnings and unaccepted_count == 0:
        note = f" (all {len(accepted_warnings)} reviewer-accepted)"
    elif accepted_warnings:
        note = f" ({unaccepted_count} unreviewed, {len(accepted_warnings)} reviewer-accepted)"
    return result(name, ok, f"{errors} errors, {warnings} warnings{note} "
                  f"({summary.get('tracks')} tracks, {summary.get('vias')} vias)", report)


def added_waivers(base_text: str | None, current_text: str | None) -> list[dict]:
    """Waivers in the current file that the base does not hold word for word: new ones, and changed ones."""
    base = json.loads(base_text)["waivers"] if base_text else []
    current = json.loads(current_text)["waivers"] if current_text else []
    return [waiver for waiver in current if waiver not in base]


def check_waivers(base_commit: str | None) -> dict:
    """G4 passes with "no waiver added": the electrical audit applies the board's waivers file itself, so a change
    that adds a problem and its waiver together would otherwise pass. A new waiver is the owner's decision."""
    name = "No electrical waiver added (G4)"
    if not WAIVERS:
        return result(name, True, "skipped: this board's description names no electrical audit, so no waivers")
    if not base_commit:
        return result(name, False, "could not find where this change left its base, so its waivers cannot be compared")
    base_text = None
    if run(["git", "cat-file", "-e", f"{base_commit}:{WAIVERS}"]).returncode == 0:
        shown = run(["git", "show", f"{base_commit}:{WAIVERS}"])
        if shown.returncode != 0:
            return tool_failure(name, shown, None)
        base_text = shown.stdout
    current = ROOT / WAIVERS
    try:
        added = added_waivers(base_text, current.read_text(encoding="utf-8") if current.is_file() else None)
    except (ValueError, KeyError, TypeError) as error:
        return result(name, False, f"could not compare the waivers: {error}")
    return result(name, not added,
                  f"none added or changed since {base_commit[:8]}" if not added else
                  f"{len(added)} added or changed since {base_commit[:8]}: "
                  + "; ".join(f"{w.get('rule')} \"{w.get('match')}\"" for w in added)
                  + ". A new waiver is the owner's decision")


def check_copper_origin(base: str) -> dict:
    """Every track, arc, via and pour this change adds came through the Routing card's routing run, or the owner's
    own record on that card accepted it (scripts/board_copper_origin.py). The committed board is the one read."""
    name = "Copper origin"
    try:
        board = boards.chosen(ROOT, {boards.ENV: str(DESCRIPTION.file)})
        found = board_copper_origin.unaccounted(ROOT, DESCRIPTION.rel("pcb"), ROOT / board.records, base,
                                                worktree=False)
    except (board_copper_origin.Unreadable, boards.BoardsError, ValueError) as error:
        return result(name, False, f"where the board's copper came from could not be read: {error}")
    if not found:
        return result(name, True, f"every track, via and pour added since {base} came through the routing run or "
                                  "was accepted by the owner")
    lines = board_copper_origin.lines(found)
    more = f"; and {len(lines) - 6} more" if len(lines) > 6 else ""
    return result(name, False, "; ".join(lines[:6]) + more + ". " + board_copper_origin.WAY_OUT)


def no_track_yet(finding: str) -> bool:
    """An audit finding of an infinite length is a track that does not exist yet."""
    return bool(re.search(r"\binf\b", finding))


def check_electrical(out: pathlib.Path, open_count: int | None = None) -> dict:
    """The board's own electrical audit, the script its description names, run with --board and --json."""
    if not ELECTRICAL:
        return result("Electrical audit (G4)", True, "skipped: this board's description names no electrical audit")
    name = ELECTRICAL["check"]
    report = cleared(out / "electrical-audit.json")
    done = kicad_offline(ELECTRICAL["script"], "--board", BOARD, "--json", report)
    audit = load(report)
    # The audit exits 1 when it fails, after writing its report; a pass counts only with exit 0.
    if audit is None or (audit.get("pass") is True and done.returncode != 0):
        return tool_failure(name, done, report)
    if audit.get("pass") is True:
        return result(name, True, "pass", report)
    findings = [f for items in (audit.get("findings") or {}).values() for f in items]
    if findings and open_count is not None and open_count > 0:
        measured = [f for f in findings if not no_track_yet(f)]
        return result(name, True, f"{len(findings) - len(measured)} wait on routing; "
                      f"open until routed: {'; '.join(measured) or 'none'}", report)
    return result(name, False, "; ".join(findings) or f"gated failures: {audit.get('gated_failures')}", report)


def check_pad_mask(out: pathlib.Path) -> dict:
    name = "Hand-solder pads have mask openings (G5)"
    report = cleared(out / "pad-mask.json")
    done = kicad_offline("scripts/kicad_report_pad_mask.py", "--board", BOARD, "--json", report)
    mask = load(report)
    if done.returncode != 0 or mask is None:
        return tool_failure(name, done, report)
    covered = mask["findings"]
    return result(name, not covered,
                  "0 covered pads" if not covered else
                  f"{len(covered)} covered: {', '.join(f['ref'] + '.' + str(f['pad']) for f in covered[:10])}", report)


def check_thermal_relief(out: pathlib.Path) -> dict:
    name = "Through-hole pads join planes by thermal spokes"
    report = cleared(out / "thermal-relief.json")
    done = kicad_offline("scripts/kicad_report_thermal_relief.py", "--board", BOARD, "--json", report)
    relief = load(report)
    if done.returncode != 0 or relief is None:
        return tool_failure(name, done, report)
    solid = relief["findings"]
    return result(name, not solid,
                  f"0 of {relief['through_hole_pads_checked']} through-hole pads on a plane joined solid" if not solid else
                  f"{len(solid)} joined solid: " + "; ".join(
                      f"{f['ref']}.{f['pad']} ({f['x_mm']}, {f['y_mm']}) on {', '.join(at['layer'] for at in f['layers'])}"
                      for f in solid[:10]), report)


def check_manual_joints(out: pathlib.Path) -> dict:
    """scripts/board_manual_joints.py on the board's text: real pins in finished holes, mask, plane settings."""
    import board_manual_joints

    name = "Hand-soldered joints: pins fit, lands open, no solid plane"
    report = cleared(out / "manual-joints.json")
    try:
        joints = board_manual_joints.report(BOARD)
    except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
        return result(name, False, f"could not run: {error}", report)
    report.write_text(json.dumps(joints, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    failing = board_manual_joints.problems(joints)
    unproven = sorted({row["part"] for row in joints["rows"] if row["verdict"] == board_manual_joints.NOT_PROVEN})
    counts = joints["counts"]
    detail = (f"{counts['rows']} joint kinds: " + ("; ".join(failing[:10]) if failing else
                                                    "0 do not fit, 0 without mask, 0 solid")
              + (f"; {counts['not_proven']} not proven ({', '.join(unproven)})" if unproven else "; 0 not proven")
              + f"; {counts.get('as_the_maker_draws_it', 0)} as the maker draws it, "
                f"{counts.get('not_fitted', 0)} not fitted")
    return result(name, joints["pass"], detail, report)


UNROUTED_PAD = re.compile(r"Pad \S+ on .* does not connect to another pad")


def check_exhaustive(out: pathlib.Path, open_count: int | None = None) -> dict:
    """The full board audit its description names, run with --board, --output and the description's args."""
    audit = DESCRIPTION.board_audit
    if not audit:
        return result("Full board audit (G6)", True, "skipped: this board's description names no full board audit")
    name = audit["check"]
    report = cleared(out / "exhaustive-board-audit.json")
    done = kicad_offline(audit["script"], "--board", BOARD, "--output", report, *audit["args"])
    audit = load(report)
    # The audit exits 1 when it finds errors, after writing its report; a pass counts only with exit 0.
    if audit is None or (audit.get("status") == "pass" and done.returncode != 0):
        return tool_failure(name, done, report)
    if audit.get("status") == "pass":
        return result(name, True, "pass", report)
    errors = audit.get("errors") or []
    real = [e for e in errors if not (open_count is not None and open_count > 0 and UNROUTED_PAD.fullmatch(e))]
    if errors and not real:
        return result(name, True, f"{len(errors)} pads wait on routing, no other error", report)
    return result(name, False, f"{len(real)} errors: {real[:5]}", report)


def merge_base(base: str) -> str | None:
    found = run(["git", "merge-base", base, "HEAD"])
    return found.stdout.strip() if found.returncode == 0 and found.stdout.strip() else None


def board_inputs_changed(base_commit: str | None) -> bool:
    """False only when git shows that nothing the board checks read has changed; any git failure runs every check."""
    if not base_commit:
        return True
    diff = run(["git", "diff", "--name-only", "-z", "--no-renames", base_commit, "HEAD"])
    if diff.returncode != 0:
        return True
    return any(path.startswith(BOARD_INPUTS) for path in diff.stdout.split("\0") if path)


def placements(board_text: str) -> dict[str, str]:
    """Each footprint's reference and position, as the saved board writes them."""
    found = {}
    for block in re.split(r"(?m)^\s*\(footprint ", board_text)[1:]:
        at = re.search(r"\(at ([^)]+)\)", block)
        reference = re.search(r'\(property "Reference" "([^"]+)"', block)
        if at and reference:
            found[reference.group(1)] = at.group(1)
    return found


def check_progress(open_count: int | None, base_open: int | None, parts_moved: int) -> dict:
    """How far routing has come. A change answers for a higher count unless parts moved, which unroutes them."""
    name = "Routing progress"
    if open_count is None:
        return result(name, False, "DRC did not provide a usable unconnected-items count")
    state = "none open" if open_count == 0 else f"{open_count} open, not ready to order"
    if base_open is None or open_count <= base_open:
        was = "" if base_open is None else f" (was {base_open})"
        return result(name, True, state + was)
    if parts_moved:
        return result(name, True, f"{state} (was {base_open}; {parts_moved} parts moved)")
    return result(name, False, f"{open_count} open, up from {base_open}, with no part moved")


def base_failures(base_commit: str | None, out: pathlib.Path) -> tuple[dict[str, str], int | None]:
    """Each check that fails on the base commit and how, from that commit's own script, and its open count.
    Nothing known if it cannot run."""
    if not base_commit:
        return {}, None
    tree, reports = out / "base-tree", out / "base-reports"
    if run(["git", "worktree", "add", "--detach", "--force", str(tree), base_commit]).returncode != 0:
        return {}, None
    try:
        # The base reads its own copy of the description: the same path, in its tree, when it is in the repository.
        env = dict(os.environ)
        try:
            env[board_description.ENV] = DESCRIPTION.file.resolve().relative_to(ROOT).as_posix()
        except ValueError:
            env[board_description.ENV] = str(DESCRIPTION.file)
        script = tree / "scripts" / "validate_board.py"
        if not script.is_file():
            script = TOOL_ROOT / "scripts" / "validate_board.py"
            env[tool_paths.ENV] = str(tree)
        subprocess.run([sys.executable, str(script), "--out", str(reports)],
                       cwd=str(tree), capture_output=True, text=True, check=False, env=env)
        summary = load(reports / "summary.json") or {}
        drc_report = load(reports / "drc.json")
        checks = summary.get("checks", [])
        reported_count = next((item["open"] for item in checks if "open" in item), None)
        count = unconnected_count(drc_report)
        return ({item["check"]: item["detail"] for item in checks if not item["ok"]},
                count if count == reported_count else None)
    finally:
        run(["git", "worktree", "remove", "--force", str(tree)])


def mark_inherited(results: list[dict], failing_on_base: dict[str, str]) -> None:
    """A failure counts against this change unless the base fails the same check with the same words."""
    for item in results:
        if not item["ok"] and failing_on_base.get(item["check"]) == item["detail"]:
            item.update(ok=True, inherited=True, detail=f"inherited, the base fails the same way: {item['detail']}")


def parts_moved(base_commit: str | None) -> int:
    """How many parts stand somewhere else than on the base commit, or were added or removed."""
    before = run(["git", "show", f"{base_commit}:{DESCRIPTION.rel('pcb')}"])
    if before.returncode != 0:
        return 0
    old, new = placements(before.stdout), placements(pathlib.Path(BOARD).read_text(encoding="utf-8"))
    return sum(old.get(reference) != new.get(reference) for reference in old.keys() | new.keys())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=pathlib.Path, required=True)
    parser.add_argument("--base", help="the branch a pull request merges into, e.g. origin/main")
    board_description.add_argument(parser)
    args = parser.parse_args()
    if args.board_description:
        use(board_description.load(args.board_description))
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)

    results = []
    base_commit = merge_base(args.base) if args.base else None
    if args.base and not board_inputs_changed(base_commit):
        results.append(result("Board checks", True, "this change touches nothing the board checks read"))
    else:
        results.append(check_erc(out))
        results.extend(check_drc(out))
        open_count = next((item["open"] for item in results if "open" in item), None)
        results.append(check_saved_fills(out))
        results.append(check_route_quality(out))
        if args.base:
            results.append(check_waivers(base_commit))
            results.append(check_copper_origin(args.base))
        results.append(check_electrical(out, open_count))
        results.append(check_pad_mask(out))
        results.append(check_thermal_relief(out))
        results.append(check_manual_joints(out))
        results.append(check_exhaustive(out, open_count))
        base_open = None
        if args.base and (open_count is None or open_count > 0 or not all(item["ok"] for item in results)):
            failing_on_base, base_open = base_failures(base_commit, out)
            mark_inherited(results, failing_on_base)
        results.append(check_progress(open_count, base_open, parts_moved(base_commit) if base_open is not None else 0))

    passed = all(item["ok"] for item in results)
    (out / "summary.json").write_text(json.dumps({"pass": passed, "checks": results}, indent=2) + "\n",
                                      encoding="utf-8")
    lines = [f"## Board checks: {'pass' if passed else 'FAIL'}", "", "| Check | Result | Detail |", "|---|---|---|"]
    lines += [f"| {item['check']} | {'inherited' if item.get('inherited') else 'pass' if item['ok'] else 'FAIL'} | {item['detail']} |"
              for item in results]
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
