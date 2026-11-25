#!/usr/bin/env python3
"""A board's description: the one file that names a board's files, folders and own checks, so the board graph's
scripts hold no board's paths or names of their own.

The description is found, in order: a `--board-description <path>` argument where a script takes one, the
environment variable BOARD_GRAPH_BOARD, else `board/board.json`. A relative path is taken from the repository root.
Every path inside the description is relative to the repository root. Standard library only, and it imports under
Python 3.9, since KiCad's Python on the Mac runs several of the scripts that read it.

  python3 scripts/board_description.py [--board-description <path>]   prints the description with absolute paths
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

import tool_paths

if __name__ == "__main__":
    tool_paths.take_argument()

ROOT = tool_paths.project_root()
DEFAULT = "board/board.json"
ENV = "BOARD_GRAPH_BOARD"
ARGUMENT = "--board-description"

# Each required key and what it holds: a path (relative to the repository root), a list of paths, text, or an object.
PATHS = ("folder", "schematic", "pcb", "kicad_project", "rules", "footprint_table", "footprint_library", "bom_export",
         "schematic_export", "owner_placement", "reports", "production", "drc_exclusions", "parts_data",
         "datasheets", "decisions")
TEXTS = ("name", "project", "package_name")
REQUIRED = TEXTS + PATHS + ("model_folders", "hand_solder_notes")
# electrical_audit and board_audit are optional: a board without its own electrical rules or its own full board audit
# leaves that key out or sets it to null, and the checks that would run it say they were skipped.
AUDIT_KEYS = {"electrical_audit": ("script", "waivers", "check"), "board_audit": ("script", "args", "check")}
NOTE_KEYS = ("manual", "factory_parts", "when_unpopulated", "when_factory")
# The other optional keys, each left out by a board that has none:
#   part_models     a Python file of the board's own STEP part models (scripts/kicad_step_model.py reads its PARTS)
#   manual_joints   {names: {reference: what the part is, in words}, clearance_note: text} for
#                   scripts/board_manual_joints.py; either field may be left out
#   routing_agent   {docs: [paths a reviewer reads first], plan: path, recipe: path, recipe_skill: text, sensitive_nets: text}
#                   for scripts/editor_step.py's prompts; any field may be left out
OPTIONAL_PATHS = ("part_models",)
JOINT_KEYS = {"names": dict, "clearance_note": str}
ROUTING_KEYS = {"docs": list, "plan": str, "recipe": str, "recipe_skill": str, "sensitive_nets": str,
                "tasks": str, "provider": str, "review_provider": str, "model": str, "review_model": str, "effort": str}


class DescriptionError(ValueError):
    """The description cannot be found, read or used; the message says which file and which key."""


def _relative(where, key, value):
    if not isinstance(value, str) or not value:
        raise DescriptionError(f"{where}: {key} must be a path, relative to the repository root")
    if pathlib.PurePosixPath(value).is_absolute() or pathlib.PureWindowsPath(value).is_absolute():
        raise DescriptionError(f"{where}: {key} must be relative to the repository root, not {value}")
    return value


def validate(data, where="board description"):
    """The description's data when it holds every required key with the right kind of value; else a
    DescriptionError naming the first key that is missing or wrong."""
    if not isinstance(data, dict):
        raise DescriptionError(f"{where}: must be a JSON object")
    for key in REQUIRED:
        if key not in data:
            raise DescriptionError(f"{where}: missing required key {key}")
    for key in TEXTS:
        if not isinstance(data[key], str) or not data[key]:
            raise DescriptionError(f"{where}: {key} must be text")
    for key in PATHS:
        _relative(where, key, data[key])
    if not isinstance(data["model_folders"], list):
        raise DescriptionError(f"{where}: model_folders must be a list of paths")
    for value in data["model_folders"]:
        _relative(where, "model_folders", value)
    for key, fields in AUDIT_KEYS.items():
        audit = data.get(key)
        if audit is None:
            continue
        if not isinstance(audit, dict):
            raise DescriptionError(f"{where}: {key} must be an object with {', '.join(fields)}")
        for field in fields:
            if field not in audit:
                raise DescriptionError(f"{where}: missing required key {key}.{field}")
        _relative(where, f"{key}.script", audit["script"])
        if key == "electrical_audit":
            _relative(where, f"{key}.waivers", audit["waivers"])
        elif not isinstance(audit["args"], list) or not all(isinstance(arg, str) for arg in audit["args"]):
            raise DescriptionError(f"{where}: {key}.args must be a list of text")
        if not isinstance(audit["check"], str) or not audit["check"]:
            raise DescriptionError(f"{where}: {key}.check must be text")
    notes = data["hand_solder_notes"]
    if not isinstance(notes, dict):
        raise DescriptionError(f"{where}: hand_solder_notes must be an object")
    for field in NOTE_KEYS:
        if field not in notes:
            raise DescriptionError(f"{where}: missing required key hand_solder_notes.{field}")
    for key in OPTIONAL_PATHS:
        if data.get(key) is not None:
            _relative(where, key, data[key])
    for key, fields in (("manual_joints", JOINT_KEYS), ("routing_agent", ROUTING_KEYS)):
        value = data.get(key)
        if value is None:
            continue
        if not isinstance(value, dict):
            raise DescriptionError(f"{where}: {key} must be an object with any of {', '.join(fields)}")
        for field, kind in fields.items():
            if field in value and not isinstance(value[field], kind):
                said = {str: "text", dict: "an object", list: "a list"}[kind]
                raise DescriptionError(f"{where}: {key}.{field} must be {said}")
    names = (data.get("manual_joints") or {}).get("names", {})
    if not all(isinstance(text, str) and text for text in names.values()):
        raise DescriptionError(f"{where}: manual_joints.names must give each reference a text")
    routing = data.get("routing_agent") or {}
    for value in routing.get("docs", []):
        _relative(where, "routing_agent.docs", value)
    if "plan" in routing:
        _relative(where, "routing_agent.plan", routing["plan"])
    if "recipe" in routing:
        _relative(where, "routing_agent.recipe", routing["recipe"])
    if "tasks" in routing:
        _relative(where, "routing_agent.tasks", routing["tasks"])
    for field in ("provider", "review_provider"):
        if routing.get(field) not in (None, "claude", "codex", "gemini"):
            raise DescriptionError(f"{where}: routing_agent.{field} needs an installed adapter (claude, codex or gemini)")
    if routing.get("effort") not in (None, "low", "medium", "high", "xhigh", "max"):
        raise DescriptionError(f"{where}: unknown routing_agent.effort")
    return data


class Description:
    """A loaded description: `rel(key)` gives a path as the description writes it (relative, with forward slashes),
    `path(key)` the absolute path, `[key]` the raw value."""

    def __init__(self, data, file, root=ROOT):
        self.data = data
        self.file = pathlib.Path(file)
        self.root = pathlib.Path(root)

    def __getitem__(self, key):
        return self.data[key]

    def get(self, key, default=None):
        return self.data.get(key, default)

    def rel(self, key):
        return pathlib.PurePosixPath(self.data[key]).as_posix()

    def path(self, key):
        return self.root / self.rel(key)

    def rels(self, key):
        return tuple(pathlib.PurePosixPath(value).as_posix() for value in self.data[key])

    def paths(self, key):
        return tuple(self.root / value for value in self.rels(key))

    @property
    def name(self):
        return self.data["name"]

    @property
    def project_folder(self):
        """The folder that holds the KiCad project, relative: what ${KIPRJMOD} names."""
        return pathlib.PurePosixPath(self.rel("kicad_project")).parent.as_posix()

    @property
    def stem(self):
        """The KiCad project's name: its files are <stem>.kicad_pcb, .kicad_sch, .kicad_pro."""
        return pathlib.PurePosixPath(self.rel("kicad_project")).stem

    @property
    def library(self):
        """The footprint library's name, as the footprint table and every board footprint name it."""
        return pathlib.PurePosixPath(self.rel("footprint_library")).stem

    @property
    def electrical_audit(self):
        """{script, waivers, check} with the paths relative, or None for a board without its own electrical audit."""
        return self.data.get("electrical_audit") or None

    @property
    def board_audit(self):
        """{script, args, check} with the script relative, or None for a board without its own full board audit."""
        return self.data.get("board_audit") or None

    @property
    def part_models(self):
        """The absolute path of the board's own part models file, or None for a board without one."""
        value = self.data.get("part_models")
        return self.root / pathlib.PurePosixPath(value).as_posix() if value else None

    @property
    def manual_joints(self):
        """{names, clearance_note} as the description gives them; {} for a board that gives neither."""
        return self.data.get("manual_joints") or {}

    @property
    def routing_agent(self):
        """{docs, recipe, recipe_skill, sensitive_nets} as the description gives them, the paths relative; {} for
        a board that gives none."""
        return self.data.get("routing_agent") or {}

    def inside_folder(self, key):
        """A path's place inside the board folder (the part after "<folder>/"), or None when it is outside it."""
        folder, value = self.rel("folder").rstrip("/") + "/", self.rel(key)
        return value[len(folder):] if value.startswith(folder) else None

    def as_absolute(self):
        """The description with each path absolute, for printing."""
        out = dict(self.data)
        for key in PATHS:
            out[key] = str(self.path(key))
        out["model_folders"] = [str(path) for path in self.paths("model_folders")]
        for key in AUDIT_KEYS:
            if out.get(key):
                out[key] = dict(out[key], script=str(self.root / out[key]["script"]))
                if "waivers" in out[key]:
                    out[key]["waivers"] = str(self.root / out[key]["waivers"])
        if self.part_models:
            out["part_models"] = str(self.part_models)
        if out.get("routing_agent"):
            routing = dict(out["routing_agent"])
            if "docs" in routing:
                routing["docs"] = [str(self.root / value) for value in routing["docs"]]
            if "plan" in routing:
                routing["plan"] = str(self.root / routing["plan"])
            if "recipe" in routing:
                routing["recipe"] = str(self.root / routing["recipe"])
            out["routing_agent"] = routing
        return out


def find(given=None, root=ROOT, environ=None):
    """The description's file: `given` (a script's --board-description), else BOARD_GRAPH_BOARD, else the first
    board the project lists (scripts/boards.py), else the default. A relative path is taken from `root`."""
    environ = os.environ if environ is None else environ
    chosen = given or environ.get(ENV)
    if not chosen:
        import boards  # here, not above: boards reads this module's names
        chosen = boards.chosen(root, environ).description
    path = pathlib.Path(chosen)
    return path if path.is_absolute() else pathlib.Path(root) / path


def load(given=None, root=ROOT, environ=None):
    """The description found by find(), read and validated."""
    file = find(given, root, environ)
    try:
        data = json.loads(file.read_text(encoding="utf-8-sig"))
    except OSError as error:
        raise DescriptionError(f"board description {file} cannot be read: {error}") from None
    except ValueError as error:
        raise DescriptionError(f"board description {file} is not JSON: {error}") from None
    return Description(validate(data, str(file)), file, root)


def add_argument(parser):
    """The --board-description argument, for a script that parses its arguments."""
    parser.add_argument(ARGUMENT, metavar="PATH", help=f"the board's description; BOARD_GRAPH_BOARD or {DEFAULT} "
                                                     "by default")
    return parser


def main(argv=None):
    parser = add_argument(argparse.ArgumentParser(description=__doc__.split("\n\n")[0]))
    args = parser.parse_args(argv)
    try:
        description = load(args.board_description)
    except DescriptionError as error:
        print(error, file=sys.stderr)
        return 1
    print(json.dumps(description.as_absolute(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
