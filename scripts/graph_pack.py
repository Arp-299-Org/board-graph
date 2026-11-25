#!/usr/bin/env python3
"""The pack a project's graph runs with: everything the graph's engine takes from a kind of work, so the engine
itself (scripts/board_graph.py, the page, the launcher) holds none of it.

A pack gives:

  actions         the card scripts `next` runs, {an action's name: Action(run, reads)}
  later           the names of actions a card may name that have no script yet
  commands        {an action's name: the command the one on the card runs, which records the outcome itself}
  views           {a view's name: (a function from the view file's text to a hash, the view in words)}: what a card
                  with unchanged_pass compares with its last pass
  view_file       the one file the views read, relative to the project root; None when the pack has no view
  reports         the folder a card's newest reports are kept under, one folder per card. With the pack "none"
                  in a project with several graphs each graph has its own: {name} in the path stands for the
                  graph's name; without it the first graph keeps the folder and another graph's is
                  <reports>.<its name>, as its records are
  branch_folders  folders whose files go on a branch and a pull request
  written         paths a run writes and never its source: the launcher leaves them out of what it watches
  notes           the one file of running notes a graph keeps, relative to the project root, with {name} standing
                  for the graph's name in a project with several: the page shows its text; None for no notes
  module          the Python file the pack's scripts are in, for messages; None when the pack has no scripts
  gates           {an action's name: Gate(run, way_out)}: what a card with that action must show before it records
                  pass or more_work. `run` takes the project root and returns a line for each thing that did not
                  come through the card's own tool (none: the card may record); `way_out` says what to do instead.
                  Only the owner's own record passes a gate that has findings. `warning`, when given, is said
                  in every brief of the card

The electronics pack (scripts/board_graph_actions.py) is the first one: a circuit board in KiCad, its files named
by the board's description. A project that is not a circuit board names the pack "none" and gives the folders
itself; its cards' scripts are then its own (PROJECT_ACTIONS), on any track.

The pack is named by tools/board_graph_pack.json, or the file BOARD_GRAPH_PACK names:

  {"pack": "electronics"}
  {"pack": "none", "reports": "reports", "branch_folders": [], "written": [], "notes": "notes/{name}.md"}

A project with no such file runs the electronics pack, as every project did before packs. With "none", `reports`
is required and the two lists and `notes` are optional. Standard library only, and it imports under Python 3.9.

  python3 scripts/graph_pack.py     prints the pack in use and what it gives
"""
from __future__ import annotations

import collections
import hashlib
import importlib
import importlib.util
import json
import os
import pathlib
import sys

import tool_paths

if __name__ == "__main__":
    tool_paths.take_argument()

ROOT = tool_paths.project_root()
DEFAULT = "tools/board_graph_pack.json"
ENV = "BOARD_GRAPH_PACK"
ELECTRONICS = "electronics"
NONE = "none"
# Each pack with scripts of its own, and the module under scripts/ that gives it (its pack() function).
MODULES = {ELECTRONICS: "board_graph_actions"}

Action = collections.namedtuple("Action", "run reads")
Gate = collections.namedtuple("Gate", "run way_out warning", defaults=("",))

# A project's own card scripts: an optional Python file of the project's, never the tool's, so a card on any track
# (firmware, an app) can have a script `next` runs. It sets ACTIONS, a dict from an action's name to (function,
# reads): the function takes the project root and returns {"outcome": "pass" | "needs_lead", "findings": [...],
# "detail": {...}}, and reads names the project paths it reads, relative to the root. In a project with several
# graphs the one file serves them all: a function that takes a second parameter called `graph` is given the name
# of the graph whose card it runs for, and {name} in a path it reads stands for that name. A name the pack already
# uses is refused, so a project never replaces a tool script.
PROJECT_ACTIONS = "scripts/board_graph_project_actions.py"


class PackError(ValueError):
    """The pack file is missing a key, names an unknown pack or gives a value of the wrong shape."""


def sha256(data):
    """The SHA-256 every graph script records, as hex."""
    return hashlib.sha256(data).hexdigest()


def file_sha256(path):
    return sha256(pathlib.Path(path).read_bytes())


def run_action(run, root, graph=None):
    """Run a card's script on the project at `root`: with the graph's name when the function takes `graph`."""
    import inspect
    if "graph" in inspect.signature(run).parameters:
        return run(root, graph=graph)
    return run(root)


def project_actions(path, taken=()):
    """The card scripts the project's file at `path` names, as {name: Action}; none when the project has no such
    file. ValueError, in a sentence that names the file, when it does not set ACTIONS as documented or names an
    action the tool has (`taken`)."""
    path = pathlib.Path(path)
    if not path.is_file():
        return {}
    spec = importlib.util.spec_from_file_location("board_graph_project_actions", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # so the page can show a function's source and constants (what_runs)
    spec.loader.exec_module(module)
    named = getattr(module, "ACTIONS", None)
    if not isinstance(named, dict):
        raise ValueError(f"{path} does not set ACTIONS, a dict from an action's name to (function, reads)")
    out = {}
    for name, value in named.items():
        if not isinstance(name, str) or not name:
            raise ValueError(f"{path} names an action {name!r}, which is not a name")
        if name in taken:
            raise ValueError(f"{path} names the action {name}, which the board graph tool has: choose another name")
        if not isinstance(value, (tuple, list)) or len(value) != 2 or not callable(value[0]):
            raise ValueError(f"{path}: the action {name} is not (function, reads)")
        reads = value[1]
        if isinstance(reads, str) or not all(isinstance(item, str) and item for item in reads):
            raise ValueError(f"{path}: what the action {name} reads is not a list of project paths")
        out[name] = Action(value[0], tuple(reads))
    return out


class Pack:
    """What the engine takes from a kind of work (the module's docstring names each part)."""

    def __init__(self, name, reports, actions=None, later=(), commands=None, views=None, view_file=None,
                 branch_folders=(), written=(), module=None, notes=None, gates=None, approval_sources=None, release_checks=None, legacy_release=None, legacy_pass=None, upkeep=None):
        self.name = name
        self.reports = reports
        self.actions = {} if actions is None else actions
        self.later = tuple(later)
        self.commands = {} if commands is None else commands
        self.views = {} if views is None else views
        self.view_file = view_file
        self.branch_folders = tuple(branch_folders)
        self.written = tuple(written)
        self.module = module
        self.notes = notes
        self.gates = {} if gates is None else gates
        self.approval_sources = approval_sources or {}
        self.release_checks = release_checks or {}
        self.legacy_release = legacy_release or {}
        self.legacy_pass = legacy_pass or {}
        self.upkeep = upkeep

    def reports_for(self, name=None, first=True):
        """The reports folder of the graph called `name` (None in a project with one graph), `first` saying
        whether it is the first listed: its own folder for each graph of a project that is not a board; a board's
        is the one its description names."""
        if "{name}" in self.reports:
            if not name:
                raise PackError(f"reports is {self.reports}, and {{name}} stands for a graph's name: this project "
                                "lists no graphs")
            return self.reports.replace("{name}", name)
        if self.name == NONE and name and not first:
            return f"{self.reports}.{name}"
        return self.reports

    def actions_for(self, name=None):
        """The card scripts as the graph called `name` runs them: {name} in a path a script reads is that name.
        ValueError when a path says {name} and the project lists no graphs."""
        if not any("{name}" in path for action in self.actions.values() for path in action.reads):
            return self.actions  # nothing to name: the pack's own dict, as every project had it
        out = {}
        for key, action in self.actions.items():
            if any("{name}" in path for path in action.reads) and not name:
                raise ValueError(f"the action {key} reads a path with {{name}}, a graph's name: this project lists "
                                 "no graphs")
            out[key] = Action(action.run, tuple(path.replace("{name}", name or "") for path in action.reads))
        return out

    def notes_path(self, name=None):
        """The notes file of the graph called `name` (None in a project with one graph), or None for no notes."""
        if not self.notes or ("{name}" in self.notes and not name):
            return None
        return self.notes.replace("{name}", name or "")

    def view_hash(self, root, name):
        """The named view of the saved view file under `root`."""
        return self.views[name][0]((pathlib.Path(root) / self.view_file).read_text(encoding="utf-8"))


def find(root=ROOT, environ=None):
    """The pack file: BOARD_GRAPH_PACK, else the default. A relative path is taken from `root`."""
    environ = os.environ if environ is None else environ
    path = pathlib.Path(environ.get(ENV) or DEFAULT).expanduser()
    return path if path.is_absolute() else pathlib.Path(root) / path


def _folder(where, key, value):
    path = pathlib.PurePosixPath(value) if isinstance(value, str) else None
    if not value or path is None or path.is_absolute() or ".." in path.parts or "\\" in value:
        raise PackError(f"{where}: {key} must be a path relative to the repository root, with forward slashes")
    return value.rstrip("/")


def read(root=ROOT, environ=None):
    """The pack file's data, validated: {"pack": "electronics"} when the project has no such file."""
    file = find(root, environ)
    if not file.is_file():
        return {"pack": ELECTRONICS}
    try:
        data = json.loads(file.read_text(encoding="utf-8-sig"))
    except OSError as error:
        raise PackError(f"pack file {file} cannot be read: {error}") from None
    except ValueError as error:
        raise PackError(f"pack file {file} is not JSON: {error}") from None
    where = str(file)
    if not isinstance(data, dict) or data.get("pack") not in (*MODULES, NONE):
        raise PackError(f"{where}: pack must be one of {', '.join((*MODULES, NONE))}")
    if data["pack"] != NONE:
        extra = sorted(set(data) - {"pack"})
        if extra:
            raise PackError(f"{where}: the {data['pack']} pack takes its folders from its own files, so "
                            f"{', '.join(extra)} cannot be set here")
        return data
    extra = sorted(set(data) - {"pack", "reports", "branch_folders", "written", "notes", "actions", "upkeep"})
    if extra:
        raise PackError(f"{where}: unknown key {', '.join(extra)}")
    if "reports" not in data:
        raise PackError(f"{where}: reports is missing: the folder the cards' reports are kept under")
    out = {"pack": NONE, "reports": _folder(where, "reports", data["reports"])}
    for key in ("branch_folders", "written"):
        value = data.get(key, [])
        if not isinstance(value, list):
            raise PackError(f"{where}: {key} must be a list of paths")
        out[key] = [_folder(where, f"{key} entry", item) for item in value]
    if data.get("notes") is not None:
        out["notes"] = _folder(where, "notes", data["notes"])
    if data.get("actions") is not None:
        out["actions"] = _folder(where, "actions", data["actions"])
    if data.get("upkeep") is not None:
        if data["upkeep"] != "local":
            raise PackError(f"{where}: upkeep must be 'local'")
        out["upkeep"] = data["upkeep"]
    return out


_LOADED = {}  # a pack is loaded once per project root and pack file: its scripts' module is imported once


def _project_approval_sources(path, taken=()):
    """Read optional APPROVAL_SOURCES from the already-loaded project actions module, if present."""
    module = sys.modules.get("board_graph_project_actions")
    target = pathlib.Path(path)
    if module is None or not target.is_file():
        return {}
    mod_file = getattr(module, "__file__", None)
    if not mod_file or pathlib.Path(mod_file).resolve() != target.resolve():
        return {}
    named = getattr(module, "APPROVAL_SOURCES", None)
    if named is None:
        return {}
    if not isinstance(named, dict):
        raise ValueError(f"{path} APPROVAL_SOURCES must be a dict mapping source name to callable")
    out = {}
    for name, fn in named.items():
        if not isinstance(name, str) or not name:
            raise ValueError(f"{path} names an approval source {name!r}, which is not a name")
        if name in taken:
            raise ValueError(f"{path} names approval source {name}, which the board graph tool has")
        if not callable(fn):
            raise ValueError(f"{path}: approval source {name} is not callable")
        out[name] = fn
    return out


def load(root=ROOT, environ=None):
    """The pack this project runs with, its own card scripts (PROJECT_ACTIONS) added to the pack's."""
    key = (str(pathlib.Path(root).resolve()), str(find(root, environ)))
    if key not in _LOADED:
        _LOADED[key] = _load(root, environ)
    return _LOADED[key]


def _load(root, environ):
    data = read(root, environ)
    actions_rel = data.get("actions") or PROJECT_ACTIONS
    if data["pack"] == NONE:
        pack = Pack(NONE, data["reports"], branch_folders=data["branch_folders"], written=data["written"],
                    notes=data.get("notes"), upkeep=data.get("upkeep"))
    else:
        pack = importlib.import_module(MODULES[data["pack"]]).pack()
    actions_path = pathlib.Path(root) / actions_rel
    pack.actions.update(project_actions(actions_path, (*pack.actions, *pack.later, *pack.commands)))
    pack.approval_sources.update(_project_approval_sources(actions_path, tuple(pack.approval_sources)))
    return pack


def main():
    try:
        pack = load()
    except (PackError, ValueError) as error:
        sys.exit(str(error))
    print(f"pack: {pack.name}" + (f" ({pack.module})" if pack.module else ""))
    print(f"reports: {pack.reports}")
    for label, items in (("card scripts", sorted(pack.actions)), ("named, no script yet", pack.later),
                         ("commands", sorted(pack.commands)), ("views", sorted(pack.views)),
                         ("on a branch", pack.branch_folders), ("written by a run", pack.written)):
        print(f"{label}: {', '.join(items) if items else 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
