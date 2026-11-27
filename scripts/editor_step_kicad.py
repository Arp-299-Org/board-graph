#!/usr/bin/env python3
"""KiCad editor lifecycle, locking, screenshots, revert, and saved-board checks for editor_step."""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import zipfile

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import tool_paths  # noqa: E402
import validate_board  # noqa: E402
from routing_evidence import parse_task_connections, saved_pours, step_connections  # noqa: E402

TOOL_ROOT = tool_paths.TOOL_ROOT
sys.path.insert(0, str(TOOL_ROOT / "tools" / "kicad"))
from kicad_install import KICAD_PCBNEW as PCBNEW  # noqa: E402


def _facade():
    mod = sys.modules.get("editor_step")
    if mod is not None:
        return mod
    main_mod = sys.modules.get("__main__")
    if main_mod is not None and pathlib.Path(getattr(main_mod, "__file__", "") or "").name == "editor_step.py":
        return main_mod
    return sys.modules[__name__]


def screen_locked() -> bool:
    """A locked screen takes every window away from the agent, and nothing here can unlock it."""
    if sys.platform.startswith("linux"):
        return False
    f = _facade()
    found = f.subprocess.run(["ioreg", "-n", "Root", "-d1"], capture_output=True, text=True).stdout
    return bool(re.search(r'CGSSessionScreenIsLocked"?\s*=\s*Yes', found))


def is_locked(path: pathlib.Path) -> bool:
    """Whether the board file is locked against editor writes."""
    st = path.stat()
    if sys.platform == "darwin":
        return bool(getattr(st, "st_flags", 0))
    return not bool(st.st_mode & 0o200)


def _picture_linux(f, pid: int, out: pathlib.Path, board_name=None) -> None:
    if board_name:
        import mac
        windows = [w for w in mac.windows() if w["pid"] == pid and mac.pcb_window_title(w["title"], board_name)]
        if len(windows) != 1 or f.screen_locked():
            raise RuntimeError(f"cannot capture one unlocked PCB Editor window for {board_name}")
        wid = str(windows[0]["number"])
        f.subprocess.run(["xdotool", "windowactivate", "--sync", wid], capture_output=True, check=False)
        f.time.sleep(0.2)
        f.subprocess.run(["import", "-window", wid, str(out)], check=True)
        with out.open("rb") as image:
            if image.read(8) != b"\x89PNG\r\n\x1a\n":
                raise ValueError(f"invalid editor capture: {out.name}")
        return
    f.subprocess.run(["import", "-window", "root", str(out)], check=False)


def picture_of_editor(pid: int, out: pathlib.Path, board_name=None) -> None:
    """The screen with this editor in front: another window on top would hide the board from a reviewer."""
    f = _facade()
    if sys.platform.startswith("linux"):
        _picture_linux(f, pid, out, board_name)
        return
    if board_name:
        import mac
        windows = [w for w in mac.windows() if w["pid"] == pid and mac.pcb_window_title(w["title"], board_name)]
        if len(windows) != 1 or f.screen_locked():
            raise RuntimeError(f"cannot capture one unlocked PCB Editor window for {board_name}")
        # macOS can show a newly launched editor while a windowless application still owns keyboard focus.
        mac.osascript(f'tell application "System Events" to set frontmost of {mac.process(str(pid))} to true')
        f.time.sleep(0.4)
        f.subprocess.run(["screencapture", "-x", "-l", str(windows[0]["number"]), str(out)], check=True)
        with out.open("rb") as image:
            if image.read(8) != b"\x89PNG\r\n\x1a\n":
                raise ValueError(f"invalid editor capture: {out.name}")
        return
    f.subprocess.run(
        ["osascript", "-e", f'tell application "System Events" to set frontmost of (first process whose unix id is {pid}) to true'],
        capture_output=True, check=False,
    )
    f.time.sleep(1)
    f.subprocess.run(["screencapture", "-x", str(out)], check=False)


def layer_pictures(socket: str, pid: int, folder: pathlib.Path, tag: str, board_name=None, extra_layers=()) -> list:
    """One picture with each outer copper layer (and any requested inner copper layers) in front."""
    from kipy import KiCad
    from kipy.proto.board.board_types_pb2 import BoardLayer
    from kipy.util.board_layer import layer_from_canonical_name
    f = _facade()
    board = KiCad(socket_path=socket, timeout_ms=60000).get_board()
    pictures = []
    previous_layer = board.get_active_layer()
    targets = [("top", BoardLayer.BL_F_Cu), ("bottom", BoardLayer.BL_B_Cu)]
    seen = {BoardLayer.BL_F_Cu, BoardLayer.BL_B_Cu}
    for layer_name in extra_layers:
        layer_id = layer_from_canonical_name(layer_name)
        if layer_id not in seen:
            seen.add(layer_id)
            targets.append((layer_name.replace(".", "_"), layer_id))
    try:
        for name, layer in targets:
            board.set_active_layer(layer)
            f.time.sleep(0.5)
            pictures.append(folder / f"{tag}-{name}.png")
            f.picture_of_editor(pid, pictures[-1], board_name)
    finally:
        board.set_active_layer(previous_layer)
    return pictures


def sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def lock(path: pathlib.Path, locked: bool) -> None:
    f = _facade()
    if sys.platform.startswith("linux"):
        mode = path.stat().st_mode
        os.chmod(path, (mode & ~0o222) if locked else (mode | 0o200))
        return
    f.subprocess.run(["chflags", "uchg" if locked else "nouchg", str(path)], check=True)


def board_path(project_path: str, board_filename: str) -> pathlib.Path:
    """The full path of the board an editor holds, from its document: KiCad gives the project as its folder or its
    .kicad_pro, and the board by file name only. A board of the same name in another folder is another board."""
    project = pathlib.PurePosixPath(project_path)
    return pathlib.Path(str((project.parent if project.suffix == ".kicad_pro" else project) / board_filename))


def same_board(held: pathlib.Path, path: pathlib.Path) -> bool:
    return held.resolve() == path.resolve()  # /tmp is /private/tmp on the Mac


def held_board(board) -> pathlib.Path:
    return board_path(str(board.document.project.path), board.document.board_filename)


def editor_board(socket: str, save: bool = False, refill: bool = False) -> pathlib.Path:
    """The full path of the board the editor at this socket holds, refilling and saving it first if asked. Raises if it
    does not answer."""
    from kipy import KiCad
    board = KiCad(socket_path=socket, timeout_ms=60000).get_board()
    if refill:
        board.refill_zones()
    if save:
        board.save()
    return _facade().held_board(board)


def running_editors(pgrep_lines: str) -> list:
    """The pids of PCB Editors in `pgrep -lf pcbnew` output: the editor's own program, not a line that only names it."""
    found = []
    for line in pgrep_lines.splitlines():
        pid, _, command = line.strip().partition(" ")
        program = command.split(" ", 1)[0]
        if pid.isdigit() and (program == "pcbnew" or program.endswith("/pcbnew")):
            found.append(int(pid))
    return found


def refuse_if_editor_running() -> None:
    """The Mac's editor has one user at a time: never open a second beside another session's."""
    f = _facade()
    found = f.running_editors(f.subprocess.run(["pgrep", "-lf", "pcbnew"], capture_output=True, text=True).stdout)
    if found:
        raise SystemExit(f"a PCB Editor is already running on this Mac (pid {', '.join(map(str, found))}), and the editor has one "
                         "user at a time: nothing was opened and nothing ran")


def open_editor(path: pathlib.Path) -> tuple:
    """Start the editor on the board and wait until it answers about that file, by its full path."""
    f = _facade()
    proc = f.subprocess.Popen([getattr(f, "PCBNEW", PCBNEW), str(path)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, start_new_session=True)
    own = pathlib.Path(f"/tmp/kicad/api-{proc.pid}.sock")
    for _ in range(90):
        f.time.sleep(1)
        socket = f"ipc://{own if own.exists() else '/tmp/kicad/api.sock'}"
        try:
            if f.same_board(f.editor_board(socket), path):
                return proc, socket
        except Exception:
            continue
    proc.kill()
    raise SystemExit(f"the editor did not answer about {path} in 90 seconds")


def close_editor(proc) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def file_copper(text: str) -> set:
    """The uuid of every track, arc and via in a board file."""
    import kicad_sexp
    ids = set()
    for item in kicad_sexp.parse(text)[1:]:
        if isinstance(item, list) and item and item[0] in ("segment", "arc", "via"):
            ids |= {part[1] for part in item if isinstance(part, list) and len(part) > 1 and part[0] == "uuid"}
    return ids


def editor_matches(socket: str, path: pathlib.Path, text: str) -> dict:
    """Whether the editor holds this board, by its full path, and this text: its copper read back over IPC by id, and
    every item as KiCad writes it."""
    from kipy import KiCad
    import kicad_reload_board
    f = _facade()
    board = KiCad(socket_path=socket, timeout_ms=60000).get_board()
    same = f.same_board(f.held_board(board), path)
    held = {str(item.id.value) for item in [*board.get_tracks(), *board.get_vias()]}
    wanted = f.file_copper(text)
    items = kicad_reload_board.board_items(board.get_as_string()) == kicad_reload_board.board_items(text)
    return {"same_board": same, "copper_same": same and held == wanted, "copper_in_editor": len(held), "copper_in_file": len(wanted),
            "only_in_editor": len(held - wanted), "only_in_file": len(wanted - held), "items_same": items}


def put_back(path: pathlib.Path, copy: pathlib.Path, proc, socket: str) -> tuple:
    """Put the board file back from the copy and make the open editor hold it again: IPC revert first, and close and
    reopen the editor only if the editor then does not match the file. Returns (what was done, proc, socket)."""
    from kipy import KiCad
    f = _facade()
    if f.is_locked(path):
        f.lock(path, False)
    shutil.copyfile(copy, path)
    text = path.read_text(encoding="utf-8")
    done: dict = {"file_same_as_copy": f.sha(path) == f.sha(copy)}
    if proc.poll() is None:
        try:
            board = KiCad(socket_path=socket, timeout_ms=60000).get_board()
            if f.same_board(f.held_board(board), path):
                board.revert()
                done.update(how="revert", **f.editor_matches(socket, path, text))
                if done["copper_same"] and done["items_same"] and f.sha(path) == f.sha(copy):
                    return done, proc, socket
        except Exception as error:
            done["revert_error"] = str(error)[-300:]
    f.close_editor(proc)
    proc, socket = f.open_editor(path)
    done.update(how="reopened", pid=proc.pid, **f.editor_matches(socket, path, text))
    return done, proc, socket


def not_idle(proc, socket: str, path: pathlib.Path) -> str:
    """Why the editor cannot take the next step, or "" when it is idle and clean and holds this board."""
    from kipy import KiCad
    import kicad_reload_board
    import mac
    f = _facade()
    if proc.poll() is not None:
        return "the editor has closed"
    try:
        board = KiCad(socket_path=socket, timeout_ms=10000).get_board()
        held = f.held_board(board)
        if not f.same_board(held, path):
            return f"the editor holds {held}, not {path}"
        board.get_active_layer()  # lightweight query refused while a tool is armed or a modal dialog is open
    except Exception as error:
        if "busy" in str(error).lower():
            return "KiCad is busy: a tool is armed or a dialog is open"
        return f"the editor does not answer: {str(error)[-200:]}"
    try:
        titles = [title for title in kicad_reload_board.editor_titles(proc.pid) if title]
    except RuntimeError as error:
        return str(error)
    board_title = f"{path.stem} {chr(0x2014)} PCB Editor"
    accepted_titles = {board_title, f"{path.stem} - PCB Editor"}
    if any(title.startswith("*") for title in titles):
        return f"the editor has unsaved edits: {titles}"
    if sum(mac.pcb_window_title(title, path.stem) for title in titles) != 1:
        return f"the board's window was not found by its title {board_title!r} exactly once among {titles}"
    if len(titles) > 1:
        return f"another window of the editor is open: {[title for title in titles if title not in accepted_titles]}"
    return ""


def expected_connections(task: str) -> list[list[str]]:
    return parse_task_connections(task)


def expected_connection(task: str):
    targets = parse_task_connections(task)
    if not targets:
        return None
    if len(targets) > 1:
        raise ValueError("Task lists multiple connections; use expected_connections()")
    return targets[0]


def saved_connection(path, target, output, centered=False):
    vb = getattr(_facade(), "validate_board", validate_board)
    options = ["--centered-exits"] if centered else []
    done = vb.kicad_offline("scripts/kicad_check_connection.py", path, *target, output, *options)
    if done.returncode:
        raise ValueError("Could not check the saved connection: " + (done.stderr or done.stdout)[-400:].strip())
    value = json.loads(output.read_text(encoding="utf-8"))["connected"]
    if type(value) is not bool:
        raise ValueError("Invalid saved connection report")
    return value


def saved_connections(path, requests):
    """Check one or more (target, output, centered) pairs, batching into one pcbnew.LoadBoard when unmocked."""
    if not requests:
        return []
    f = _facade()
    if len(requests) == 1 or f.saved_connection is not saved_connection:
        return [f.saved_connection(path, target, output, centered) for target, output, centered in requests]
    vb = getattr(f, "validate_board", validate_board)
    spec_path = pathlib.Path(requests[0][1]).parent / f".conn-batch-{os.getpid()}-{int(time.time() * 1000)}.json"
    payload = [
        {
            "start": target[0],
            "end": target[1],
            "net": target[2],
            "output": str(output),
            "centered_exits": bool(centered),
        }
        for target, output, centered in requests
    ]
    spec_path.write_text(json.dumps(payload), encoding="utf-8")
    try:
        done = vb.kicad_offline("scripts/kicad_check_connection.py", path, "--batch", spec_path)
        if done.returncode:
            raise ValueError("Could not check the saved connection: " + (done.stderr or done.stdout)[-400:].strip())
    finally:
        spec_path.unlink(missing_ok=True)
    values = []
    for _target, output, _centered in requests:
        value = json.loads(pathlib.Path(output).read_text(encoding="utf-8"))["connected"]
        if type(value) is not bool:
            raise ValueError("Invalid saved connection report")
        values.append(value)
    return values


def _write_status(message: str) -> None:
    """Best-effort single-line live status update for routing_run.py wait() observability."""
    target = os.environ.get("BOARD_GRAPH_STATUS_FILE")
    if not target:
        return
    try:
        pathlib.Path(target).write_text(f"{time.strftime('%H:%M:%S')} {message}\n", encoding="utf-8")
    except OSError:
        pass


def save_candidate(path, folder, tag, socket, pid, connection=None, *, pours=None, extra_layers=()):
    """Archive the exact saved geometry and its checks before rollback can replace it."""
    f = _facade()
    connections = [] if connection is None else (connection if isinstance(connection, list) else [connection])
    if (not connections or not all(isinstance(c, dict) for c in connections)) and not pours:
        raise ValueError("Candidate archive requires at least one saved connection or pour check")
    archive = folder / f"candidate-{tag}.zip"
    pic_kwargs = {"extra_layers": extra_layers} if extra_layers else {}
    pictures = f.layer_pictures(socket, pid, folder, tag + "-candidate", path.stem, **pic_kwargs)
    board = path.read_bytes()
    board_sha = hashlib.sha256(board).hexdigest()
    if any(c.get("saved_sha256") != board_sha for c in connections) or any(
        p.get("saved_sha256") != board_sha for p in (pours or ())
    ):
        raise ValueError("Saved board changed after the named check")
    report_names = (
        [f"connection-{tag}.json"]
        if len(connections) == 1
        else [f"connection-{tag}-{idx}.json" for idx in range(1, len(connections) + 1)]
    )
    reports = {}
    closeups = {}
    for report_name in report_names:
        report = folder / report_name
        report_bytes = report.read_bytes()
        reports[report.name] = report_bytes
        for number, entry in enumerate(json.loads(report_bytes).get("pad_exits", []), 1):
            name = f"{report.stem}-pad{number}.png"
            if entry.get("picture") != name:
                raise ValueError(f"Expected {name} in the saved pad-exit report")
            data = (folder / name).read_bytes()
            if not data.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError(f"Invalid pad close-up: {name}")
            closeups[name] = data
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("candidate.kicad_pcb", board)
        for name, data in reports.items():
            bundle.writestr(name, data)
        for name, data in closeups.items():
            bundle.writestr(name, data)
        for source in [folder / f"quality-{tag}.json", folder / "drc-end.json", *pictures]:
            bundle.write(source, source.name)
    return {"step_folder": folder.name, "archive": archive.name, "sha256": f.sha(archive),
            "board_sha256": board_sha}, pictures


def _archive_partial_candidate(path, folder, tag, socket, pid, connection=None):
    """Best-effort candidate zip preservation before reverting clean partial copper."""
    f = _facade()
    if connection is not None and (folder / f"quality-{tag}.json").is_file():
        try:
            cand, _pics = f.save_candidate(path, folder, tag, socket, pid, connection)
            return cand
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            pass
    archive = folder / f"candidate-{tag}.zip"
    try:
        pictures = f.layer_pictures(socket, pid, folder, tag + "-candidate", path.stem)
    except Exception:
        pictures = []
    board = path.read_bytes()
    board_sha = hashlib.sha256(board).hexdigest()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("candidate.kicad_pcb", board)
        sources = [
            folder / f"quality-{tag}.json",
            folder / "drc-end.json",
            *pictures,
            *sorted(folder.glob(f"connection-{tag}*.json")),
            *sorted(folder.glob(f"connection-{tag}*-pad*.png")),
        ]
        for source in sources:
            if source.is_file():
                bundle.write(source, source.name)
    return {"step_folder": folder.name, "archive": archive.name, "sha256": f.sha(archive), "board_sha256": board_sha}


def final_connections(path, results, folder):
    """Later routing work must preserve each earlier named connection on the PCB being handed over."""
    f = _facade()
    prior = [row for row in results if row.get("kept")][:-1]
    pairs = {}
    before = f.sha(path)
    entries = []
    requests = []
    for i, row in enumerate(prior):
        conns = step_connections(row)
        if not conns:
            continue
        centered = bool(row.get("pad_exits"))
        multi = row.get("connections") is not None
        for j, connection in enumerate(conns, 1):
            target = [connection[k] for k in ("from", "to", "net")]
            report = folder / (f"delivery-pair-{i + 1}-{j}.json" if multi else f"delivery-pair-{i + 1}.json")
            entries.append((row, centered, multi, target, report))
            requests.append((target, report, centered))
    checked = f.saved_connections(path, requests)
    grouped: dict[str, list[list[str]]] = {}
    for (row, centered, multi, target, report), is_connected in zip(entries, checked):
        if not is_connected:
            raise ValueError(f"Previously kept connection reopened: {row['task']}")
        if centered:
            pads = json.loads(report.read_bytes()).get("pad_exits", [])
            if [p["pad"] for p in pads] != target[:2] or any(p.get("issues") != [] for p in pads):
                raise ValueError(f"Previously kept pad exit changed: {row['task']}")
        grouped.setdefault(row["task"], []).append(target)
        pairs[row["task"]] = grouped[row["task"]] if multi else grouped[row["task"]][0]
    if f.sha(path) != before:
        raise ValueError("The final PCB changed during the connection recheck")
    return {"board_sha256": before, "connections": pairs}


def final_pours(path, results):
    """Later steps must preserve each earlier declared copper pour as a filled zone on the final PCB."""
    f = _facade()
    prior = [row for row in results if row.get("kept")][:-1]
    declared = {row["task"]: [{"layers": p["layers"], "net": p["net"]} for p in row["pours"]]
                for row in prior if row.get("pours")}
    if not declared:
        return None
    before = f.sha(path)
    text = path.read_text(encoding="utf-8")
    pours_map = {}
    for task_name, specs in declared.items():
        checked = saved_pours(text, specs, before)
        missing = [f"{p['net']} ({','.join(p['layers'])})" for p in checked if p["zone_count"] == 0 or not p["filled"]]
        if missing:
            raise ValueError(f"Previously kept pour disappeared or emptied on final PCB ({task_name}): {', '.join(missing)}")
        pours_map[task_name] = [{"layers": p["layers"], "net": p["net"]} for p in checked]
    if f.sha(path) != before:
        raise ValueError("The final PCB changed during the pour recheck")
    return {"board_sha256": before, "pours": pours_map}
