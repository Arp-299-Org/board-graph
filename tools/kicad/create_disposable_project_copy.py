#!/usr/bin/env python3
"""Create a uniquely named, internally consistent disposable KiCad project.

The source is never modified.  The destination must not already exist.  KiCad
project metadata is updated before first open so a GUI save does not introduce
one-time filename normalization that can masquerade as an unrelated edit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import shutil
import sys


REQUIRED_PROJECT_SUFFIXES = (".kicad_pro", ".kicad_sch", ".kicad_pcb")
RENAMED_PROJECT_SUFFIXES = REQUIRED_PROJECT_SUFFIXES + (".kicad_dru",)
SKIP_NAMES = {".history", "__pycache__"}


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def should_skip(path: pathlib.Path) -> bool:
    return (
        path.name in SKIP_NAMES
        or path.name.startswith("~")
        or path.name.endswith(".lck")
    )


def copy_project(
    source_dir: pathlib.Path,
    source_base: str,
    destination_dir: pathlib.Path,
    destination_base: str,
    *,
    schematic_only: bool = False,
) -> dict[str, object]:
    source_dir = source_dir.resolve()
    destination_dir = destination_dir.resolve()
    if not source_dir.is_dir():
        raise FileNotFoundError(f"source directory not found: {source_dir}")
    if destination_dir.exists():
        raise FileExistsError(f"destination already exists: {destination_dir}")
    if source_dir in destination_dir.parents:
        raise ValueError(f"destination must not be inside source directory: {destination_dir}")
    for name in (source_base, destination_base):
        if name in ("", ".", "..") or any(character in name for character in "/\\:"):
            raise ValueError(f"project base name must be a single filename: {name!r}")

    suffixes = (".kicad_pro", ".kicad_sch") if schematic_only else REQUIRED_PROJECT_SUFFIXES
    required = [source_dir / f"{source_base}{suffix}" for suffix in suffixes]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"missing source project files: {missing}")

    entries: list[tuple[pathlib.Path, str]] = []
    destination_names: dict[str, pathlib.Path] = {}
    for child in source_dir.iterdir():
        if should_skip(child):
            continue
        destination_name = child.name
        for suffix in RENAMED_PROJECT_SUFFIXES:
            if child.name == f"{source_base}{suffix}":
                destination_name = f"{destination_base}{suffix}"
                break
        name_key = destination_name.casefold()
        if name_key in destination_names:
            raise FileExistsError(
                f"source entries {destination_names[name_key]} and {child} "
                f"would both copy to {destination_name}"
            )
        destination_names[name_key] = child
        entries.append((child, destination_name))

    destination_dir.mkdir(parents=True)
    for child, destination_name in entries:
        destination = destination_dir / destination_name
        if child.is_dir():
            shutil.copytree(child, destination, ignore=shutil.ignore_patterns(".history", "~*", "*.lck"))
        else:
            shutil.copy2(child, destination)

    project_path = destination_dir / f"{destination_base}.kicad_pro"
    project = json.loads(project_path.read_text(encoding="utf-8"))
    project.setdefault("meta", {})["filename"] = f"{destination_base}.kicad_pro"
    schematic = project.setdefault("schematic", {})
    if not isinstance(schematic, dict):
        raise ValueError("project 'schematic' field is not an object")
    sheets = schematic.get("top_level_sheets", [])
    if not isinstance(sheets, list):
        raise ValueError("project 'schematic.top_level_sheets' field is not a list")
    for sheet in sheets:
        if sheet.get("filename") == f"{source_base}.kicad_sch":
            sheet["filename"] = f"{destination_base}.kicad_sch"
        if sheet.get("name") == source_base:
            sheet["name"] = destination_base
    project_path.write_text(json.dumps(project, indent=2) + "\n", encoding="utf-8")

    files = sorted(path for path in destination_dir.rglob("*") if path.is_file())
    return {
        "schema": 1,
        "source_dir": str(source_dir),
        "source_base": source_base,
        "destination_dir": str(destination_dir),
        "destination_base": destination_base,
        "files": [
            {
                "path": str(path.relative_to(destination_dir)),
                "bytes": path.stat().st_size,
                "sha256": sha256(path),
            }
            for path in files
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_dir", type=pathlib.Path)
    parser.add_argument("source_base")
    parser.add_argument("destination_dir", type=pathlib.Path)
    parser.add_argument("destination_base")
    parser.add_argument("--manifest", type=pathlib.Path)
    parser.add_argument("--schematic-only", action="store_true", help="Allow a starter project before its PCB exists.")
    args = parser.parse_args()

    manifest = copy_project(
        args.source_dir,
        args.source_base,
        args.destination_dir,
        args.destination_base,
        schematic_only=args.schematic_only,
    )
    rendered = json.dumps(manifest, indent=2) + "\n"
    if args.manifest:
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
