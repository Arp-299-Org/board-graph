"""Setuptools build hook to bundle tool scripts and starter assets in non-editable wheels."""
from __future__ import annotations

import json
import pathlib
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py

ROOT = pathlib.Path(__file__).resolve().parent


class BuildWithToolData(build_py):
    """Copy manifest-listed files, starter files, and metadata into _board_graph_data."""

    def run(self) -> None:
        super().run()
        if getattr(self, "editable_mode", False):
            return
        manifest_path = ROOT / "tools" / "board_graph_tool.json"
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        paths = sorted(
            set(data.get("files", {}))
            | set(data.get("starter", []))
            | {"tools/board_graph_tool.json", "UPGRADING.md"}
        )
        dest_root = pathlib.Path(self.build_lib) / "_board_graph_data"
        for rel in paths:
            src = ROOT / rel
            if not src.is_file():
                continue
            dst = dest_root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


setup(py_modules=["board_graph_entry"], cmdclass={"build_py": BuildWithToolData})
