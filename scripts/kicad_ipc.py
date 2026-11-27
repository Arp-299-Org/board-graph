#!/usr/bin/env python3
"""Shared KiCad IPC connection, project identity verification, pad geometry, and board cache helpers.

Lazy-imports `kipy` inside functions and methods so offline scripts and unit tests without `kipy`
can import cleanly under Python 3.9+.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import uuid

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import kicad_rules_geom as rg  # noqa: E402
import tool_paths  # noqa: E402

TOOL_ROOT = tool_paths.TOOL_ROOT
INVOKE_KICAD_CLI = TOOL_ROOT / "tools" / "kicad" / "invoke_kicad_cli.py"


def invoke_kicad_cli_path() -> pathlib.Path:
    """Return the path to `tools/kicad/invoke_kicad_cli.py` under `TOOL_ROOT`."""
    return INVOKE_KICAD_CLI


def cached_drc_report(
    project_dir: pathlib.Path,
    board_name: str,
    pcb_text: str,
    cli_path: pathlib.Path,
    *,
    cache_dir: pathlib.Path | None = None,
) -> dict:
    """Run `kicad-cli pcb drc --refill-zones` or return a cached JSON report keyed by inputs SHA-256."""
    project_dir = pathlib.Path(project_dir)
    hasher = hashlib.sha256()
    hasher.update(f"{cli_path}\0{board_name}\0".encode("utf-8"))
    for suffix in (".kicad_pro", ".kicad_dru"):
        src = project_dir / f"{board_name}{suffix}"
        hasher.update(suffix.encode("ascii") + b"\0")
        if src.is_file():
            hasher.update(src.read_bytes())
        hasher.update(b"\0")
    hasher.update(pcb_text.encode("utf-8"))
    key = hasher.hexdigest()

    resolved_cache = (
        pathlib.Path(cache_dir)
        if cache_dir is not None
        else pathlib.Path(tempfile.gettempdir()) / "board-graph-drc-cache"
    )
    cache_file = resolved_cache / f"{key}.json"
    if cache_file.is_file():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cached = None
        if isinstance(cached, dict) and "violations" in cached and "unconnected_items" in cached:
            return cached

    with tempfile.TemporaryDirectory(prefix="board-graph-lay-drc-") as folder:
        tmp_dir = pathlib.Path(folder)
        for suffix in (".kicad_pro", ".kicad_dru"):
            src = project_dir / f"{board_name}{suffix}"
            if src.exists():
                shutil.copy(src, tmp_dir / src.name)
        pcb = tmp_dir / f"{board_name}.kicad_pcb"
        pcb.write_text(pcb_text, encoding="utf-8")
        out = tmp_dir / "drc.json"
        done = subprocess.run(
            [
                sys.executable,
                str(cli_path),
                "pcb",
                "drc",
                "--format",
                "json",
                "--severity-all",
                "--refill-zones",
                "--output",
                str(out),
                str(pcb),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=600,
        )
        if not out.is_file():
            raise RuntimeError("kicad-cli DRC failed: " + (done.stderr or done.stdout).strip()[-300:])
        report = json.loads(out.read_text(encoding="utf-8"))

    try:
        resolved_cache.mkdir(parents=True, exist_ok=True)
        staged = resolved_cache / f".{key}.{uuid.uuid4().hex}.tmp"
        staged.write_text(json.dumps(report), encoding="utf-8")
        os.replace(staged, cache_file)
    except OSError:
        pass
    return report


def project_identity(path: pathlib.Path) -> tuple[pathlib.Path, str]:
    """Resolve a project directory, `.kicad_pro`, or `.kicad_pcb` path to `(project_dir, board_filename)`."""
    resolved = path.resolve()
    if resolved.suffix == ".kicad_pro":
        return resolved.parent, resolved.with_suffix(".kicad_pcb").name
    if resolved.suffix == ".kicad_pcb":
        return resolved.parent, resolved.name
    if resolved.is_dir():
        boards = sorted(resolved.glob("*.kicad_pcb"))
        if len(boards) != 1:
            raise RuntimeError(
                f"Expected exactly one .kicad_pcb under {resolved}; found {len(boards)}"
            )
        return resolved, boards[0].name
    raise RuntimeError(f"Project must be a directory or .kicad_pro file: {resolved}")


def project_dir(path: pathlib.Path) -> pathlib.Path:
    """Return the project directory for a `.kicad_pro`, `.kicad_pcb`, or directory path."""
    return project_identity(path)[0]


def verify_live_project(board, expected_project: pathlib.Path) -> tuple[pathlib.Path, str]:
    """Verify the connected live KiCad board matches `expected_project`."""
    expected_directory, expected_board = project_identity(expected_project)
    live_project = pathlib.Path(str(board.document.project.path)).resolve()
    live_directory = live_project.parent if live_project.suffix == ".kicad_pro" else live_project
    if live_directory != expected_directory or board.document.board_filename != expected_board:
        raise RuntimeError(
            "Refusing wrong live project: "
            f"project={board.document.project.path} board={board.document.board_filename}"
        )
    return expected_directory, expected_board


def connect_board(
    timeout_ms: int = 180_000,
    *,
    expected_project: pathlib.Path | None = None,
    socket_path: str | None = None,
):
    """Connect to the open PCB Editor via KiCad IPC and optionally verify its project/board identity."""
    from kipy import KiCad

    kwargs: dict = {"timeout_ms": timeout_ms}
    if socket_path is not None:
        kwargs["socket_path"] = socket_path
    board = KiCad(**kwargs).get_board()
    if expected_project is not None:
        verify_live_project(board, expected_project)
    return board


def pad_copper_box(pad) -> tuple[float, float, float, float]:
    """A kipy pad's copper as an upright box around its rotated, offset outline, in mm."""
    stack = pad.padstack
    copper_layers = getattr(stack, "copper_layers", None) or ()
    angle_deg = float(getattr(getattr(stack, "angle", None), "degrees", 0.0) or 0.0)
    px = float(pad.position.x) / rg.NM
    py = float(pad.position.y) / rg.NM
    if not copper_layers:
        return px, py, px, py
    shape_layer = copper_layers[0]
    size = shape_layer.size
    sx, sy = float(size.x) / rg.NM, float(size.y) / rg.NM
    offset = getattr(shape_layer, "offset", None)
    ox = float(offset.x) / rg.NM if offset is not None else 0.0
    oy = float(offset.y) / rg.NM if offset is not None else 0.0
    cx, cy = rg.local_to_world(px, py, angle_deg, ox, oy) if (ox or oy) else (px, py)
    hx, hy = rg.rotated_pad_aabb_half(sx, sy, angle_deg)
    return cx - hx, cy - hy, cx + hx, cy + hy


def extract_pad_geometry(
    pad,
    canonical_name,
    copper_filter,
    *,
    shape_names: dict | None = None,
    default_shape: str = "unknown",
) -> dict | None:
    """Extract normalized world-mm copper geometry and net info for one kipy pad."""
    stack = pad.padstack
    layers = tuple(canonical_name(layer) for layer in stack.layers if copper_filter(layer))
    if not layers or not stack.copper_layers:
        return None
    shape_layer = stack.copper_layers[0]
    size = shape_layer.size
    sx, sy = float(size.x) / rg.NM, float(size.y) / rg.NM
    if sx <= 0 or sy <= 0:
        return None
    angle_deg = float(stack.angle.degrees)
    offset = getattr(shape_layer, "offset", None)
    ox = float(offset.x) / rg.NM if offset is not None else 0.0
    oy = float(offset.y) / rg.NM if offset is not None else 0.0
    px, py = float(pad.position.x) / rg.NM, float(pad.position.y) / rg.NM
    center = rg.local_to_world(px, py, angle_deg, ox, oy) if (ox or oy) else (px, py)
    if shape_names is None:
        from kipy.proto.board.board_types_pb2 import PadStackShape

        shape_names = {
            PadStackShape.PSS_RECTANGLE: "rect",
            PadStackShape.PSS_ROUNDRECT: "roundrect",
            PadStackShape.PSS_OVAL: "oval",
            PadStackShape.PSS_CIRCLE: "circle",
        }
    net_obj = getattr(pad, "net", None)
    net_name = str(getattr(net_obj, "name", "") or "") if net_obj is not None else ""
    return {
        "layers": layers,
        "center": center,
        "position": (px, py),
        "size": (sx, sy),
        "angle_deg": angle_deg,
        "shape": shape_names.get(shape_layer.shape, default_shape),
        "round_ratio": float(getattr(shape_layer, "corner_rounding_ratio", 0.0) or 0.0),
        "net": net_name,
    }


class KipyBoardCacheMixin:
    """Shared session-level caches for footprints, layers, nets, netclasses, and copper queries."""

    def init_ipc_caches(self) -> None:
        """Initialize empty cache slots on a live or mock adapter instance."""
        self._ipc_caches_ready = True
        self._nets = None
        self._netclasses = None
        self._netclasses_bulk_loaded = False
        self._footprints = None
        self._footprints_by_ref = None
        self._enabled_layers = None
        self._raw_zones: dict = {}
        self._pad_obstacles: list[dict] | None = None
        self._clearance_floor: tuple[bool, float | None] = (False, None)
        self._cached_tracks = None
        self._cached_vias = None
        self._cached_zones = None

    def _ensure_ipc_caches(self) -> None:
        if not getattr(self, "_ipc_caches_ready", False):
            self.init_ipc_caches()

    def invalidate_copper_cache(self) -> None:
        """Invalidate mutable copper caches (tracks, vias, zones) after board edits."""
        self._ensure_ipc_caches()
        self._cached_tracks = None
        self._cached_vias = None
        self._cached_zones = None
        self._raw_zones = {}

    def _wait_ready_if_present(self) -> None:
        if not hasattr(self.board, "get_active_layer"):
            return
        wait_fn = getattr(self, "wait_ready", None)
        if callable(wait_fn):
            wait_fn()

    def all_footprints(self) -> list:
        """Return cached board footprints and populate ref index in a single IPC call."""
        self._ensure_ipc_caches()
        if self._footprints is None:
            self._wait_ready_if_present()
            fps = list(self.board.get_footprints())
            by_ref: dict[str, list] = {}
            for fp in fps:
                ref = fp.reference_field.text.value if getattr(fp, "reference_field", None) else ""
                by_ref.setdefault(ref, []).append(fp)
            self._footprints = fps
            self._footprints_by_ref = by_ref
        return self._footprints

    def footprints_for_ref(self, reference: str) -> list:
        self.all_footprints()
        return (self._footprints_by_ref or {}).get(reference) or []

    def enabled_layer_ids(self) -> set:
        """Return cached set of raw enabled layer IDs."""
        self._ensure_ipc_caches()
        raw = getattr(self, "_enabled", None)
        if raw is None:
            route = getattr(self, "route", None)
            if route is not None:
                raw = getattr(route, "_enabled", None)
        if raw is None:
            self._wait_ready_if_present()
            raw = set(self.board.get_enabled_layers())
        self._enabled = raw
        route = getattr(self, "route", None)
        if route is not None:
            route._enabled = raw
        return raw

    def net_map(self) -> dict:
        """Return cached `{net_name: kipy_net}` map in a single IPC call."""
        self._ensure_ipc_caches()
        if self._nets is None:
            self._wait_ready_if_present()
            self._nets = {net.name: net for net in self.board.get_nets() if net.name}
        return self._nets

    @staticmethod
    def _extract_netclass_dict(
        info,
        default_track: float = rg.DEFAULT_TRACK_MM,
        default_via_dia: float = rg.DEFAULT_VIA_DIA_MM,
        default_via_drill: float = rg.DEFAULT_VIA_DRILL_MM,
        default_clearance: float = rg.DEFAULT_CLEARANCE_MM,
    ) -> dict:
        tw, vd, vdr, cl = default_track, default_via_dia, default_via_drill, default_clearance
        if info is not None:
            if getattr(info, "track_width", 0) > 0:
                tw = rg.to_mm(info.track_width)
            if getattr(info, "via_diameter", 0) > 0:
                vd = rg.to_mm(info.via_diameter)
            if getattr(info, "via_drill", 0) > 0:
                vdr = rg.to_mm(info.via_drill)
            if getattr(info, "clearance", 0) > 0:
                cl = rg.to_mm(info.clearance)
        return {
            "track_width_mm": tw,
            "via_diameter_mm": vd,
            "via_drill_mm": vdr,
            "clearance_mm": cl,
        }

    def netclass_defaults(self, net_name: str) -> dict:
        """Return cached netclass settings, bulk-fetching all netclasses once per instance."""
        self._ensure_ipc_caches()
        if self._netclasses is None:
            self._netclasses = {}
        nets = self.net_map()
        if not self._netclasses_bulk_loaded:
            self._netclasses_bulk_loaded = True
            net_objs = list(nets.values())
            if net_objs:
                try:
                    bulk = self.board.get_netclass_for_nets(net_objs) or {}
                    for name, info in bulk.items():
                        self._netclasses[name] = self._extract_netclass_dict(info)
                except Exception:
                    pass
        if net_name not in self._netclasses:
            net_obj = nets.get(net_name)
            info = None
            if net_obj is not None:
                try:
                    info = (self.board.get_netclass_for_nets(net_obj) or {}).get(net_name)
                except Exception:
                    info = None
            self._netclasses[net_name] = self._extract_netclass_dict(info)
        return self._netclasses[net_name]

    def raw_tracks_and_vias(self) -> tuple[list, list]:
        """Return cached `(tracks, vias)` fetched once until `invalidate_copper_cache()`."""
        self._ensure_ipc_caches()
        if self._cached_tracks is None or self._cached_vias is None:
            self._wait_ready_if_present()
            self._cached_tracks = list(self.board.get_tracks())
            self._cached_vias = list(self.board.get_vias())
        return self._cached_tracks, self._cached_vias

    def raw_zones(self) -> list:
        """Return cached `get_zones()` fetched once until `invalidate_copper_cache()`."""
        self._ensure_ipc_caches()
        if self._cached_zones is None:
            self._wait_ready_if_present()
            self._cached_zones = list(self.board.get_zones())
        return self._cached_zones
