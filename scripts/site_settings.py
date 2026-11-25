#!/usr/bin/env python3
"""The project's machine setup: the one file that names the KiCad machine (its SSH host and key, its checkout, the
steps folder and its lock, the prefix for branch worktrees, its cache, the prefix of its disposable KiCad copies),
the graph page's address and launchd job, and where the state database lives, so the board graph's scripts hold no
machine's names of their own.

The file is found, in order: the environment variable BOARD_GRAPH_SITE, else `tools/board_graph_site.json`. A
relative path is taken from the repository root, which on the KiCad machine itself is the checkout the script runs
from. Paths on the KiCad machine may start with ~/, the home of the user SSH logs in as; `state_database` is relative
to `checkout`. Standard library only, and it imports under Python 3.9, since the KiCad machine runs these scripts too.

A project that runs on one machine (a laptop alone, no KiCad machine) says so with `"one_machine": true` and
leaves `kicad_machine` out: the file then needs `state_database` only, the database is a file under the repository's own main checkout on this machine, and
nothing is reached over SSH. `graph_page` is then optional, and so is its `launchd_label`; without an address the
page is served at http://127.0.0.1:8000.

Environment variables have neutral `BOARD_GRAPH_*` / `KICAD_*` names.

  python3 scripts/site_settings.py     prints the settings in use and the file they came from
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import tool_paths

if __name__ == "__main__":
    tool_paths.take_argument()

from no_window import KWARGS as NO_WINDOW  # noqa: E402

ROOT = tool_paths.project_root()
DEFAULT = "tools/board_graph_site.json"
ENV = "BOARD_GRAPH_SITE"

# Every required key, by section, and what it holds. A key outside a section is a top-level one.
MACHINE = ("ssh_host", "ssh_key", "checkout", "steps", "editor_lock", "worktree_prefix", "cache", "disposable_copies")
PAGE = ("address", "launchd_label")
REQUIRED = {"kicad_machine": MACHINE, "graph_page": PAGE}
TOP = ("state_database",)
# graph_page.python is optional: the Python the page's launchd job runs under on the KiCad machine, when it is
# not macOS's own.
PAGE_PYTHON = "/usr/bin/python3"
# One machine: the page's address when the file gives none, and the machine's folders, which only the KiCad tools
# use and a project without a KiCad machine never reaches.
ONE_MACHINE_PAGE = "http://127.0.0.1:8000"
ONE_MACHINE = {"steps": "~/.cache/board-graph/steps", "editor_lock": "~/.cache/board-graph/steps/EDITOR_BUSY.d",
               "worktree_prefix": "~/board-graph-", "cache": "~/.cache/board-graph",
               "disposable_copies": "/tmp/board-graph-copy-"}

def env_name(name, environ=None):
    """Return `name` when set to a non-empty value in `environ`, else None."""
    environ = os.environ if environ is None else environ
    return name if environ.get(name) else None


def env(name, default=None, environ=None):
    """The value of `name` in `environ` when set, else `default`."""
    environ = os.environ if environ is None else environ
    which = env_name(name, environ)
    return environ[which] if which else default


def both_names(name, value):
    """Return `{name: value}` for child-process environments."""
    return {name: value}


class SiteError(ValueError):
    """The settings cannot be found, read or used; the message says which file and which key."""


def _text(where, key, value):
    if not isinstance(value, str) or not value:
        raise SiteError(f"{where}: {key} must be text")
    return value


def validate(data, where="site settings"):
    """The settings' data when it holds every required key as text; else a SiteError naming the first key that is
    missing or wrong."""
    if not isinstance(data, dict):
        raise SiteError(f"{where}: must be a JSON object")
    one_machine = data.get("one_machine", False)
    if not isinstance(one_machine, bool):
        raise SiteError(f"{where}: one_machine must be true or false")
    if one_machine and "kicad_machine" in data:
        raise SiteError(f"{where}: one_machine is true, so kicad_machine cannot be set: the project names a KiCad "
                        "machine or runs on one machine, not both")
    for section, keys in REQUIRED.items():
        if one_machine and section == "kicad_machine":
            continue
        if one_machine and section == "graph_page":  # optional, and so is each of its keys
            if not isinstance(data.get(section, {}), dict):
                raise SiteError(f"{where}: {section} must be an object with {', '.join(keys)}")
            for key in keys:
                if key in data.get(section, {}):
                    _text(where, f"{section}.{key}", data[section][key])
            continue
        if section not in data:
            raise SiteError(f"{where}: missing required key {section}")
        if not isinstance(data[section], dict):
            raise SiteError(f"{where}: {section} must be an object with {', '.join(keys)}")
        for key in keys:
            if key not in data[section]:
                raise SiteError(f"{where}: missing required key {section}.{key}")
            _text(where, f"{section}.{key}", data[section][key])
    if not one_machine and "tool_checkout" in data["kicad_machine"]:
        _text(where, "kicad_machine.tool_checkout", data["kicad_machine"]["tool_checkout"])
    for key in TOP:
        if key not in data:
            raise SiteError(f"{where}: missing required key {key}")
        _text(where, key, data[key])
    if pathlib.PurePosixPath(data["state_database"]).is_absolute():
        raise SiteError(f"{where}: state_database must be relative to " +
                        ("the repository's main checkout" if one_machine else "kicad_machine.checkout"))
    page = data.get("graph_page", {})
    if "://" not in page.get("address", ONE_MACHINE_PAGE):
        raise SiteError(f"{where}: graph_page.address must be a URL, such as http://host:8000")
    if "python" in page:
        _text(where, "graph_page.python", data["graph_page"]["python"])
    return data


class Settings:
    """Loaded settings: the values as attributes, `[key]` the raw data."""

    def __init__(self, data, file, root=ROOT):
        self.data = data
        self.file = pathlib.Path(file)
        # One machine: no KiCad machine is named, so this machine holds the database, in the repository's main
        # checkout, and nothing is reached over SSH.
        self.one_machine = bool(data.get("one_machine"))
        page = data.get("graph_page", {})
        if self.one_machine:
            machine = dict(ONE_MACHINE, ssh_host="", ssh_key="", checkout=main_checkout(root))
            page = dict({"address": ONE_MACHINE_PAGE, "launchd_label": ""}, **page)
        else:
            machine = data["kicad_machine"]
        self.ssh_host = machine["ssh_host"]
        self.ssh_key = pathlib.Path(machine["ssh_key"]).expanduser()  # a path on this machine
        self.checkout = machine["checkout"]  # paths on the KiCad machine, ~ its user's home
        self.steps = machine["steps"]
        self.editor_lock = machine["editor_lock"]
        self.worktree_prefix = machine["worktree_prefix"]
        self.cache = machine["cache"]
        self.disposable_copies = machine["disposable_copies"]
        self.page_address = page["address"].rstrip("/")
        self.launchd_label = page["launchd_label"]
        self.page_python = page.get("python", PAGE_PYTHON)  # a path on the KiCad machine
        self.state_database = data["state_database"]
        self.tool_checkout = None if self.one_machine else machine.get("tool_checkout")

    def __getitem__(self, key):
        return self.data[key]

    def worktree(self, branch):
        """The default worktree on the KiCad machine for `branch`: the prefix and the branch's last part."""
        return f"{self.worktree_prefix}{branch.rsplit('/', 1)[-1]}"


def main_checkout(root=ROOT):
    """The repository's main checkout, as text: the folder that holds its .git folder, which every worktree of the
    repository shares. `root` itself when git does not say."""
    try:
        done = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=str(root),
                              capture_output=True, text=True, timeout=30, check=False, **NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return pathlib.Path(root).resolve().as_posix()
    common = pathlib.Path(done.stdout.strip()) if done.returncode == 0 and done.stdout.strip() else None
    if common is not None and common.name == ".git":
        return common.parent.as_posix()
    return pathlib.Path(root).resolve().as_posix()


def find(root=ROOT, environ=None):
    """The settings file: BOARD_GRAPH_SITE, else the default. A relative path is taken from `root`."""
    environ = os.environ if environ is None else environ
    path = pathlib.Path(environ.get(ENV) or DEFAULT).expanduser()
    return path if path.is_absolute() else pathlib.Path(root) / path


def load(root=ROOT, environ=None):
    """The settings found by find(), read and validated."""
    file = find(root, environ)
    try:
        data = json.loads(file.read_text(encoding="utf-8-sig"))
    except OSError as error:
        raise SiteError(f"site settings {file} cannot be read: {error}") from None
    except ValueError as error:
        raise SiteError(f"site settings {file} is not JSON: {error}") from None
    return Settings(validate(data, str(file)), file, root)


def main():
    try:
        settings = load()
    except SiteError as error:
        print(error, file=sys.stderr)
        return 1
    print(f"from {settings.file}")
    print(json.dumps(settings.data, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
