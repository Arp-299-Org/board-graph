#!/usr/bin/env python3
"""The board graph tool's manifest: the one file, `tools/board_graph_tool.json`, that names the tool, its repository,
its version and every tool file with the SHA-256 of its bytes. The same file is the manifest in the tool's repository
and the pin in a project that holds a copy of the tool, so a project's copy is the tool at that version exactly.
It also lists the starter files (`starter`, paths only): the first copy of files a project then owns, such as its
graph, its settings and its AGENTS.md. `check`, `update` and `export` never read or copy them.

A hash is taken after CR LF is turned into LF, so a Windows checkout (CR LF) and a Mac checkout (LF) give the same one.
Standard library only, and it imports under Python 3.9.

  python3 scripts/board_graph_tool.py check
      every listed file is here with its listed hash; exit 1 with a line for each file that differs or is missing
  python3 scripts/board_graph_tool.py manifest --version X [--add PATH ...] [--remove PATH ...]
                                              [--add-starter PATH ...] [--remove-starter PATH ...]
      hash the listed files (with those added or removed) and write the manifest at version X, with the starter list
      (with those added or removed); a path is in one list or the other, never both
  python3 scripts/board_graph_tool.py export --to <folder> [--replace]
      write every listed file and the manifest into <folder> at the same paths (LF line endings); refuse, writing
      nothing, when the folder already has a different file at a tool file's path, unless --replace is given
  python3 scripts/board_graph_tool.py update --from <checkout of the tool's repository> [--ref <tag or commit>]
      take the tool at that ref (read with git, not from the checkout's working files): refuse when a copy here
      differs from the current manifest; else write the tool's files, delete the files it no longer lists, and write
      its manifest. Running it again changes nothing.
  python3 scripts/board_graph_tool.py starter --from <checkout of the tool's repository> [--ref <tag or commit>]
                                             [--skip PATH ...]
      write each starter file the manifest at that ref lists (read with git) where this project has no file at its
      path, and never overwrite one; print each file written and each one skipped because the project has it, to
      merge by hand. Running it again writes nothing. --skip leaves out starter files the project keeps under
      another name (its own product statement or decisions file, say), so they are not written beside its own.

Every command takes --root <folder>, the project's root (by default the repository this script is in).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from no_window import KWARGS as NO_WINDOW  # noqa: E402

ROOT = HERE.parent
MANIFEST = "tools/board_graph_tool.json"
NAME = "board-graph"
REPOSITORY = "Arp-299-Org/board-graph"
HOW_TO_CHANGE = ("A tool file is changed in the tool's repository ({repository}): change it there, run "
                 "`python3 scripts/board_graph_tool.py manifest --version <new>` and release that version, then run "
                 "`python3 scripts/board_graph_tool.py update --from <checkout of it> --ref <version>` here.")


class ToolError(ValueError):
    """The manifest or a copy cannot be used; the message says which file and why."""


def digest(data):
    """The SHA-256 of `data` with CR LF turned into LF."""
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def _checked_path(value, where):
    path = pathlib.PurePosixPath(value)
    if (not value or "\\" in value or path.is_absolute() or pathlib.PureWindowsPath(value).is_absolute()
            or ".." in path.parts or path.as_posix() != value):
        raise ToolError(f"{where}: {value!r} must be a relative path with forward slashes")
    if value == MANIFEST:
        raise ToolError(f"{where}: the manifest does not list itself")
    return value


def validate(data, where=MANIFEST):
    if not isinstance(data, dict):
        raise ToolError(f"{where}: must be a JSON object")
    for key in ("name", "repository", "version"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise ToolError(f"{where}: {key} must be text")
    files = data.get("files")
    if not isinstance(files, dict):
        raise ToolError(f"{where}: files must be an object of path and SHA-256")
    for path, value in files.items():
        _checked_path(path, where)
        if not isinstance(value, str) or len(value) != 64:
            raise ToolError(f"{where}: {path} must have a SHA-256")
    starter = data.get("starter", [])
    if not isinstance(starter, list) or not all(isinstance(path, str) for path in starter):
        raise ToolError(f"{where}: starter must be a list of paths")
    for path in starter:
        _checked_path(path, where)
        if path in files:
            raise ToolError(f"{where}: {path} is both a tool file and a starter file")
    if len(set(starter)) != len(starter):
        raise ToolError(f"{where}: starter lists a path twice")
    return data


def parse(text, where=MANIFEST):
    try:
        return validate(json.loads(text), where)
    except ValueError as error:
        if isinstance(error, ToolError):
            raise
        raise ToolError(f"{where} is not JSON: {error}") from None


def load(root):
    file = pathlib.Path(root) / MANIFEST
    try:
        text = file.read_text(encoding="utf-8")
    except OSError as error:
        raise ToolError(f"{file} cannot be read: {error}") from None
    return parse(text, str(file))


def render(data):
    """The manifest's text: keys in a fixed order, files sorted, LF line endings."""
    out = {"name": data["name"], "repository": data["repository"], "version": data["version"],
           "files": {path: data["files"][path] for path in sorted(data["files"])},
           "starter": sorted(data.get("starter", []))}
    return json.dumps(out, indent=2, ensure_ascii=False) + "\n"


def differences(root, data):
    """One line per listed file that is missing here or has another hash."""
    lines = []
    for path, expected in sorted(data["files"].items()):
        file = pathlib.Path(root) / path
        if not file.is_file():
            lines.append(f"missing: {path}")
        elif digest(file.read_bytes()) != expected:
            lines.append(f"differs from {data['name']} {data['version']}: {path}")
    return lines


def _ending(file):
    """CR LF when the file here uses it, else LF (a new file gets LF)."""
    try:
        return b"\r\n" if b"\r\n" in file.read_bytes() else b"\n"
    except OSError:
        return b"\n"


def _write(file, data, ending):
    data = data.replace(b"\r\n", b"\n")
    if ending == b"\r\n":
        data = data.replace(b"\n", b"\r\n")
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(data)


def check(root):
    data = load(root)
    lines = differences(root, data)
    if lines:
        for line in lines:
            print(line)
        print(HOW_TO_CHANGE.format(repository=data["repository"]))
        return 1
    print(f"{data['name']} {data['version']}: {len(data['files'])} files match {MANIFEST}")
    return 0


def _changed(listed, add, remove, which, flag):
    """The paths in `listed` with `remove` taken out (each must be there) and `add` put in."""
    paths = set(listed)
    for path in remove:
        if path not in paths:
            raise ToolError(f"{path} is not in the {which} list of {MANIFEST}")
        paths.discard(path)
    paths.update(_checked_path(path, flag) for path in add)
    return paths


def manifest(root, version, add=(), remove=(), add_starter=(), remove_starter=()):
    root = pathlib.Path(root)
    file = root / MANIFEST
    data = load(root) if file.exists() else {"name": NAME, "repository": REPOSITORY, "version": version, "files": {}}
    paths = _changed(data["files"], add, remove, "files", "--add")
    starter_paths = _changed(data.get("starter", []), add_starter, remove_starter, "starter", "--add-starter")
    both = sorted(paths & starter_paths)
    if both:
        raise ToolError("a path is a tool file or a starter file, never both: " + ", ".join(both))
    missing = sorted(path for path in paths | starter_paths if not (root / path).is_file())
    if missing:
        raise ToolError("listed but missing: " + ", ".join(missing))
    data = dict(data, version=version, files={path: digest((root / path).read_bytes()) for path in paths},
                starter=sorted(starter_paths))
    _write(file, render(data).encode("utf-8"), _ending(file))
    print(f"wrote {MANIFEST}: {data['name']} {version}, {len(paths)} files, {len(starter_paths)} starter files")
    return 0


def export(root, to, replace=False):
    data = load(root)
    lines = differences(root, data)
    if lines:
        raise ToolError("this checkout's copies differ from its manifest, so they are not the tool: "
                        + "; ".join(lines))
    to = pathlib.Path(to)
    # A project that already has a file at a tool file's path (an older copy of the same script, say) keeps it
    # until someone has looked: nothing is written unless --replace is given.
    theirs = [path for path in sorted(data["files"])
              if (to / path).is_file() and digest((to / path).read_bytes()) != data["files"][path]]
    if theirs and not replace:
        raise ToolError(f"refused: {len(theirs)} file(s) in {to} are at a tool file's path and differ from the tool; "
                        "nothing was written. Look at each (commit them first, so git keeps them), then run again "
                        "with --replace: " + ", ".join(theirs))
    for path in sorted(data["files"]):
        _write(to / path, (pathlib.Path(root) / path).read_bytes(), b"\n")
    _write(to / MANIFEST, render(data).encode("utf-8"), b"\n")
    print(f"exported {data['name']} {data['version']}: {len(data['files'])} files and {MANIFEST} to {to}")
    return 0


def _git(checkout, *args):
    done = subprocess.run(["git", "-C", str(checkout), *args], capture_output=True, check=False, **NO_WINDOW)
    if done.returncode:
        said = done.stderr.decode("utf-8", "replace").strip().splitlines()
        raise ToolError(f"git {' '.join(args)} in {checkout}: {said[-1] if said else 'failed'}")
    return done.stdout


def update(root, source, ref="HEAD"):
    root = pathlib.Path(root)
    current = load(root)
    local = differences(root, current)
    if local:
        raise ToolError("refused: the copies here differ from " + f"{current['name']} {current['version']} "
                        "(edited here?); " + "; ".join(local))
    commit = _git(source, "rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()
    manifest_blob = _git(source, "show", f"{commit}:{MANIFEST}").replace(b"\r\n", b"\n")
    new = parse(manifest_blob.decode("utf-8"), f"{MANIFEST} at {commit}")
    if new["name"] != current["name"]:
        raise ToolError(f"refused: {source} at {commit} is {new['name']}, not {current['name']}")
    blobs = {}
    for path, expected in sorted(new["files"].items()):
        blob = _git(source, "show", f"{commit}:{path}")
        if digest(blob) != expected:
            raise ToolError(f"refused: {path} at {commit} does not match that commit's own manifest")
        blobs[path] = blob
    written, deleted = [], []
    for path, blob in blobs.items():
        file = root / path
        if file.is_file() and digest(file.read_bytes()) == new["files"][path]:
            continue
        _write(file, blob, _ending(file))
        written.append(path)
    # A file the new version makes a starter file is the project's from then on: it stays.
    for path in sorted(set(current["files"]) - set(new["files"]) - set(new.get("starter", []))):
        (root / path).unlink()
        deleted.append(path)
    file = root / MANIFEST
    # The manifest as that version wrote it, so a key a later version adds survives an older updater.
    text = manifest_blob
    if file.read_bytes().replace(b"\r\n", b"\n") != text:
        _write(file, text, _ending(file))
        written.append(MANIFEST)
    print(f"{new['name']} {current['version']} -> {new['version']} at {commit}: "
          f"{len(written)} written, {len(deleted)} deleted")
    for path in written:
        print(f"  wrote {path}")
    for path in deleted:
        print(f"  deleted {path}")
    try:
        notes = _git(source, "show", f"{commit}:{NOTES}").decode("utf-8", "replace")
    except ToolError:
        notes = ""  # a version from before the notes file: its tag's message says what to change
    sections = upgrade_notes(notes, current["version"], new["version"])
    if sections:
        print("\nWhat this project changes in its own files, by version:")
        for version, body in sections:
            print(f"\n{version}\n\n{body}")
    return 0


NOTES = "UPGRADING.md"  # in the tool's repository only: what a project changes after taking each version


def version_key(version):
    """A version as numbers, for order: "0.12.0" is (0, 12, 0); a part that is not a number counts as 0."""
    return tuple(int(part) if part.isdigit() else 0 for part in str(version).split("."))


def upgrade_notes(text, after=None, upto=None):
    """The notes file's sections as [(version, body)], oldest first: each `## <version>` heading and the text
    under it. With `after` and `upto`, only the versions past `after` up to `upto`."""
    found, version, lines = [], None, []
    for line in text.splitlines():
        heading = re.fullmatch(r"##\s+v?(\d+(?:\.\d+)*)\s*", line)
        if heading:
            if version:
                found.append((version, "\n".join(lines).strip()))
            version, lines = heading.group(1), []
        elif version:
            lines.append(line)
    if version:
        found.append((version, "\n".join(lines).strip()))
    return sorted(((version, body) for version, body in found
                   if (after is None or version_key(version) > version_key(after))
                   and (upto is None or version_key(version) <= version_key(upto))),
                  key=lambda item: version_key(item[0]))


def notes(root, file=None, version=None):
    """Print this version's section of the notes file in `root`, the tool's repository (or of `file`, for
    `version`). ToolError when the version has none: a version is not released without saying what a project
    changes, even when it is nothing."""
    version = version or load(root)["version"]
    file = pathlib.Path(file) if file else pathlib.Path(root) / NOTES
    text = file.read_text(encoding="utf-8") if file.is_file() else ""
    section = dict(upgrade_notes(text)).get(version)
    if not section:
        raise ToolError(f"{NOTES} has no section for {version}: add `## {version}` saying what a project changes "
                        "in its own files after taking it, or that it changes nothing")
    print(section)
    return 0


def starter(root, source=None, ref="HEAD", skip=()):
    """Write each starter file of the tool at `ref` (or the installed tool root when `source` is None) where
    the project has no file at its path; never overwrite one. Prints each file written and each one skipped."""
    root = pathlib.Path(root)
    if source is None:
        commit = None
        data = load(ROOT)
    else:
        commit = _git(source, "rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()
        data = parse(_git(source, "show", f"{commit}:{MANIFEST}").decode("utf-8"), f"{MANIFEST} at {commit}")
    paths = data.get("starter", [])
    unknown = sorted(set(skip) - set(paths))
    if unknown:
        raise ToolError(f"refused: --skip names what is not a starter file of {data['name']} {data['version']}: "
                        + ", ".join(unknown) + "; nothing was written")
    blobs = {
        path: ((ROOT / path).read_bytes() if commit is None else _git(source, "show", f"{commit}:{path}"))
        for path in paths
        if path not in skip
    }
    written, kept, left = [], [], []
    for path in paths:
        file = root / path
        if path in skip:
            left.append(path)
            continue
        if file.exists() or file.is_symlink():
            kept.append(path)
            continue
        _write(file, blobs[path], b"\n")
        written.append(path)
    where = f" at {commit}" if commit is not None else ""
    print(f"{data['name']} {data['version']} starter files{where}: {len(written)} written, "
          f"{len(kept)} skipped because this project has them"
          + (f", {len(left)} left out by --skip" if left else ""))
    for path in written:
        print(f"  wrote {path}")
    for path in kept:
        merge_hint = f"; merge by hand from {commit}:{path} if it should change" if commit is not None else ""
        print(f"  skipped {path}: this project has it{merge_hint}")
    for path in left:
        print(f"  left out {path}: --skip named it")
    return 0


KEEP_ON_UNVENDOR = frozenset({"tools/board_graph_view/.gitignore"})


def _prune_empty_dirs(start: pathlib.Path, stop_at: pathlib.Path) -> None:
    cur = start
    stop_resolved = stop_at.resolve()
    while cur.resolve() != stop_resolved and cur.is_dir():
        try:
            if any(cur.iterdir()):
                break
            cur.rmdir()
        except OSError:
            break
        cur = cur.parent


def unvendor(root, mac_tool_checkout=None, dry_run=False):
    """Remove unmodified vendored tool files and manifest so a project uses an external board-graph checkout."""
    root = pathlib.Path(root)
    current = load(root)
    local = differences(root, current)
    if local:
        raise ToolError("refused: the copies here differ from " + f"{current['name']} {current['version']} "
                        "(edited here?); " + "; ".join(local))
    to_remove = [path for path in sorted(current["files"])
                 if path not in KEEP_ON_UNVENDOR and (root / path).is_file()]
    if (root / MANIFEST).is_file():
        to_remove.append(MANIFEST)
    site_file = root / "tools" / "board_graph_site.json"
    updated_site = None
    if mac_tool_checkout is not None:
        if not site_file.is_file():
            raise ToolError(f"cannot set tool_checkout: {site_file} does not exist")
        import site_settings
        try:
            site_data = json.loads(site_file.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as error:
            raise ToolError(f"site settings {site_file} cannot be read: {error}") from None
        if not isinstance(site_data, dict):
            raise ToolError(f"{site_file}: must be a JSON object")
        if site_data.get("one_machine"):
            raise ToolError(f"{site_file}: one_machine is true, so kicad_machine.tool_checkout cannot be set")
        if not isinstance(site_data.get("kicad_machine"), dict):
            raise ToolError(f"{site_file}: missing required key kicad_machine")
        site_data["kicad_machine"]["tool_checkout"] = mac_tool_checkout
        try:
            site_settings.validate(site_data, str(site_file))
        except site_settings.SiteError as error:
            raise ToolError(str(error)) from None
        updated_site = json.dumps(site_data, indent=2, ensure_ascii=False) + "\n"
    if not dry_run:
        for rel in to_remove:
            target = root / rel
            if target.is_file():
                target.unlink()
                _prune_empty_dirs(target.parent, root)
        if updated_site is not None:
            _write(site_file, updated_site.encode("utf-8"), _ending(site_file))
    verb = "would remove" if dry_run else "removed"
    print(f"unvendored {current['name']} {current['version']}: {verb} {len(to_remove)} file(s), "
          f"kept {', '.join(sorted(KEEP_ON_UNVENDOR))}")
    if updated_site is not None:
        print(f"  {'would update' if dry_run else 'updated'} tools/board_graph_site.json "
              f"(kicad_machine.tool_checkout = {mac_tool_checkout!r})")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", default=str(ROOT), help="the project's root; this repository by default")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="every listed file is here with its listed hash")
    made = commands.add_parser("manifest", help="hash the listed files and write the manifest")
    made.add_argument("--version", required=True)
    made.add_argument("--add", nargs="+", default=[], metavar="PATH")
    made.add_argument("--remove", nargs="+", default=[], metavar="PATH")
    made.add_argument("--add-starter", nargs="+", default=[], metavar="PATH")
    made.add_argument("--remove-starter", nargs="+", default=[], metavar="PATH")
    out = commands.add_parser("export", help="write every listed file and the manifest into a folder")
    out.add_argument("--to", required=True)
    out.add_argument("--replace", action="store_true",
                     help="write over files in the folder that are at a tool file's path and differ from the tool")
    take = commands.add_parser("update", help="take the tool's files at a ref of a checkout of its repository")
    take.add_argument("--from", dest="source", required=True)
    take.add_argument("--ref", default="HEAD")
    said = commands.add_parser("notes", help="in the tool's repository: this version's section of UPGRADING.md, "
                                             "what a project changes in its own files after taking it")
    said.add_argument("--file", help="the notes file to read; UPGRADING.md in the root by default")
    said.add_argument("--version", help="the version whose section to print; the manifest's by default")
    first = commands.add_parser("starter", help="write the tool's starter files this project does not have")
    first.add_argument("--from", dest="source", default=None,
                       help="optional git checkout of board-graph (defaults to this installed package)")
    first.add_argument("--ref", default="HEAD")
    first.add_argument("--skip", nargs="+", default=[], metavar="PATH",
                       help="starter files to leave out: the ones this project keeps under another name")
    drop = commands.add_parser("unvendor", help="remove unmodified vendored tool files to use external board-graph")
    drop.add_argument("--mac-tool-checkout", metavar="PATH",
                      help="set kicad_machine.tool_checkout in tools/board_graph_site.json")
    drop.add_argument("--dry-run", action="store_true", help="print what would change without deleting files")
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            return check(args.root)
        if args.command == "manifest":
            return manifest(args.root, args.version, args.add, args.remove, args.add_starter, args.remove_starter)
        if args.command == "export":
            return export(args.root, args.to, args.replace)
        if args.command == "notes":
            return notes(args.root, args.file, args.version)
        if args.command == "starter":
            return starter(args.root, args.source, args.ref, args.skip)
        if args.command == "unvendor":
            return unvendor(args.root, args.mac_tool_checkout, args.dry_run)
        return update(args.root, args.source, args.ref)
    except ToolError as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
