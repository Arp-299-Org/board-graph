#!/usr/bin/env python3
"""Dense 4-layer board integration tests across rules, geometry, IPC caching, receipts, and gates."""
from __future__ import annotations

import json
import pathlib
import shutil
import sys
import tempfile
import types
import unittest

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import board_context  # noqa: E402
import editor_step_audit  # noqa: E402
import kicad_ipc  # noqa: E402
import kicad_lay  # noqa: E402
import kicad_route  # noqa: E402
import kicad_rules_geom as rg  # noqa: E402
import kicad_sexp  # noqa: E402
import pad_exits  # noqa: E402
import routing_codex  # noqa: E402
import routing_evidence  # noqa: E402
import routing_report  # noqa: E402
import validate_board  # noqa: E402

RECEIPTS_DIR = SCRIPTS_DIR / "fixtures" / "receipts"
FIXED_SHA = "1111111111111111111111111111111111111111111111111111111111111111"

DENSE_NETS = [
    (0, ""),
    (1, "GND"),
    (2, "VCC_3V3"),
    (3, "SPI_SCK"),
    (4, "SPI_MOSI"),
    (5, "I2C_SDA"),
    (6, "I2C_SCL"),
    (7, "CLK"),
    (8, "SIG_0"),
    (9, "SIG_1"),
    (10, "SIG_2"),
    (11, "SIG_3"),
    (12, "SIG_4"),
    (13, "SIG_5"),
    (14, "SIG_6"),
    (15, "SIG_7"),
]


def _fp_block(ref: str, x: float, y: float, angle: float, pads: list[str], courtyard: bool = True) -> str:
    cy = ""
    if courtyard:
        cy = '    (fp_rect (start -2.5 -2.0) (end 2.5 2.0) (stroke (width 0.05) (type solid)) (layer "F.CrtYd"))\n'
    body = "\n".join(pads)
    return (
        f'  (footprint "Demo:Part"\n'
        f'    (layer "F.Cu")\n'
        f'    (at {x} {y} {angle})\n'
        f'    (property "Reference" "{ref}")\n'
        f'{cy}{body}\n'
        f'  )'
    )


def _pad_sexp(
    num: str,
    lx: float,
    ly: float,
    angle: float,
    sx: float,
    sy: float,
    net_id: int,
    net_name: str,
    shape: str = "roundrect",
    offset: tuple[float, float] | None = None,
) -> str:
    off = f" (offset {offset[0]} {offset[1]})" if offset else ""
    rr = " (roundrect_rratio 0.25)" if shape == "roundrect" else ""
    return (
        f'    (pad "{num}" smd {shape} (at {lx} {ly} {angle}) (size {sx} {sy}){off} '
        f'(layers "F.Cu" "F.Mask"){rr} (net {net_id} "{net_name}"))'
    )


def build_dense_board_bundle(root: pathlib.Path) -> dict[str, object]:
    """Create a realistic 4-layer dense KiCad bundle with 56+ footprints, 340+ tracks, 44+ vias, zones, and DRU."""
    board_dir = root / "board"
    board_dir.mkdir(parents=True, exist_ok=True)
    pcb_path = board_dir / "dense-demo.kicad_pcb"
    pro_path = board_dir / "dense-demo.kicad_pro"
    dru_path = board_dir / "dense-demo.kicad_dru"

    footprints: list[str] = []
    # U1 (0 deg controller) in active box [95..125, 82..108]
    u1_pads = [
        _pad_sexp("1", -2.0, -1.0, 0, 0.8, 0.45, 3, "SPI_SCK"),
        _pad_sexp("2", -2.0, 0.0, 0, 0.8, 0.45, 7, "CLK"),
        _pad_sexp("3", -2.0, 1.0, 0, 0.8, 0.45, 1, "GND"),
        _pad_sexp("4", 2.0, -1.0, 0, 0.8, 0.45, 4, "SPI_MOSI"),
        _pad_sexp("5", 2.0, 0.0, 0, 0.8, 0.45, 5, "I2C_SDA"),
        _pad_sexp("6", 2.0, 1.0, 0, 0.8, 0.45, 6, "I2C_SCL"),
    ]
    footprints.append(_fp_block("U1", 100.0, 90.0, 0.0, u1_pads))

    # U2 (90 deg rotated sensor with oval pad + roundrect pad)
    u2_pads = [
        _pad_sexp("1", -1.5, 0.5, 90, 0.9, 0.5, 3, "SPI_SCK", shape="roundrect"),
        _pad_sexp("2", 1.5, -0.5, 90, 1.2, 0.6, 5, "I2C_SDA", shape="oval"),
        _pad_sexp("3", 0.0, -1.5, 90, 0.8, 0.5, 4, "SPI_MOSI", shape="rect"),
        _pad_sexp("4", 0.0, 1.5, 90, 0.8, 0.5, 1, "GND", shape="roundrect"),
    ]
    footprints.append(_fp_block("U2", 116.0, 90.0, 90.0, u2_pads))

    # SW1 (180 deg with split dual pad "1" -> SW1.1@X1,Y1 and SW1.1@X2,Y2)
    sw1_pads = [
        _pad_sexp("1", -1.2, 0.0, 180, 1.0, 0.8, 7, "CLK", shape="rect"),
        _pad_sexp("1", 1.2, 0.0, 180, 1.0, 0.8, 7, "CLK", shape="rect"),
        _pad_sexp("2", 0.0, -1.4, 180, 1.0, 0.8, 1, "GND", shape="rect"),
    ]
    footprints.append(_fp_block("SW1", 108.0, 102.0, 180.0, sw1_pads))

    # U3 (270 deg) + R1 (45 deg)
    u3_pads = [
        _pad_sexp("1", -1.0, 0.0, 270, 0.7, 0.45, 5, "I2C_SDA", shape="roundrect"),
        _pad_sexp("2", 1.0, 0.0, 270, 0.7, 0.45, 6, "I2C_SCL", shape="roundrect"),
    ]
    footprints.append(_fp_block("U3", 120.0, 98.0, 270.0, u3_pads))
    footprints.append(_fp_block("R1", 105.0, 86.0, 45.0, [
        _pad_sexp("1", -0.8, 0.0, 45, 0.7, 0.5, 6, "I2C_SCL"),
        _pad_sexp("2", 0.8, 0.0, 45, 0.7, 0.5, 2, "VCC_3V3"),
    ]))

    # Q1..Q21: 21 isolated 2-pad pairs in clean corridor [84..190, 46..72] for 20-route batch + rollback tests
    for idx in range(21):
        col = idx % 5
        row = idx // 5
        qx = 90.0 + col * 20.0
        qy = 48.0 + row * 5.0
        net_id = 8 + (idx % 8)
        net_name = f"SIG_{idx % 8}"
        footprints.append(_fp_block(f"Q{idx + 1}", qx, qy, 0.0, [
            _pad_sexp("1", -2.5, 0.0, 0, 0.6, 0.4, net_id, net_name, shape="roundrect"),
            _pad_sexp("2", 2.5, 0.0, 0, 0.6, 0.4, net_id, net_name, shape="roundrect"),
        ]))

    # P1..P41: 41 distant peripheral parts -> 66 footprints total
    for idx in range(41):
        px = 145.0 + (idx % 7) * 12.0
        py = 118.0 + (idx // 7) * 7.0
        angle = float((idx * 45) % 360)
        net_id = 8 + (idx % 8)
        net_name = f"SIG_{idx % 8}"
        pads = [
            _pad_sexp("1", -0.8, 0.0, angle, 0.65, 0.45, net_id, net_name, shape="roundrect"),
            _pad_sexp("2", 0.8, 0.0, angle, 0.65, 0.45, 1, "GND", shape="rect"),
        ]
        footprints.append(_fp_block(f"P{idx + 1}", px, py, angle, pads))

    # 340 tracks across 4 layers: 8 in active corridor [97..122, 93..108.5] + 332 distant grid
    segments: list[str] = []
    for k, y in enumerate((93.0, 94.0, 95.0, 96.0, 105.5, 106.5, 107.5, 108.5)):
        layer = ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")[k % 4]
        segments.append(
            f'  (segment (start 97.0 {y}) (end 122.0 {y}) (width 0.25) (layer "{layer}") (net {3 + (k % 5)}))'
        )
    for k in range(332):
        row = k // 4
        layer = ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")[k % 4]
        y = 162.0 + (row % 21) * 1.8
        x0 = 142.0 + (row // 21) * 14.0
        net_id = 8 + (k % 8)
        segments.append(
            f'  (segment (start {x0} {y}) (end {x0 + 7.0} {y}) (width 0.25) (layer "{layer}") (net {net_id}))'
        )

    # 44 vias across F.Cu..B.Cu: 6 inside active box, 38 distant
    vias: list[str] = []
    for k in range(6):
        vx = 98.0 + k * 3.0
        vy = 98.0
        vias.append(f'  (via (at {vx} {vy}) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net {3 + (k % 4)}))')
    for k in range(38):
        vx = 142.0 + (k % 10) * 6.0
        vy = 201.0 + (k // 10) * 2.8
        vias.append(f'  (via (at {vx} {vy}) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net {8 + (k % 8)}))')

    zones = [
        '\t(zone (net 1) (net_name "GND")\n'
        '\t\t(layer "In1.Cu")\n'
        '\t\t(uuid "00000000-0000-0000-0000-000000000101")\n'
        '\t\t(polygon (pts (xy 82 44) (xy 243 44) (xy 243 213) (xy 82 213)))\n'
        '\t\t(filled_polygon (layer "In1.Cu") (pts (xy 82.5 44.5) (xy 242.5 44.5) (xy 242.5 212.5) (xy 82.5 212.5))))',
        '\t(zone (net 2) (net_name "VCC_3V3")\n'
        '\t\t(layer "In2.Cu")\n'
        '\t\t(uuid "00000000-0000-0000-0000-000000000102")\n'
        '\t\t(polygon (pts (xy 82 44) (xy 243 44) (xy 243 213) (xy 82 213)))\n'
        '\t\t(filled_polygon (layer "In2.Cu") (pts (xy 83.0 45.0) (xy 242.0 45.0) (xy 242.0 212.0) (xy 83.0 212.0))))',
    ]

    net_lines = "\n".join(f'  (net {nid} "{name}")' for nid, name in DENSE_NETS)
    board_text = (
        '(kicad_pcb (version 20241229) (generator "pcbnew")\n'
        '  (layers\n'
        '    (0 "F.Cu" signal)\n'
        '    (1 "In1.Cu" power)\n'
        '    (2 "In2.Cu" signal)\n'
        '    (31 "B.Cu" signal)\n'
        '    (44 "Edge.Cuts" user)\n'
        '  )\n'
        f'{net_lines}\n'
        '  (gr_rect (start 80 42) (end 245 215) (stroke (width 0.1) (type solid)) (layer "Edge.Cuts"))\n'
        + "\n".join(footprints) + "\n"
        + "\n".join(segments) + "\n"
        + "\n".join(vias) + "\n"
        + "\n".join(zones) + "\n"
        ')\n'
    )
    pro_text = json.dumps({
        "net_settings": {
            "classes": [
                {"name": "Default", "clearance": 0.2, "track_width": 0.25, "via_diameter": 0.6, "via_drill": 0.3},
                {"name": "Power", "clearance": 0.25, "track_width": 0.5, "via_diameter": 0.8, "via_drill": 0.4},
            ],
            "netclass_assignments": {"GND": "Power", "VCC_3V3": "Power"},
        }
    })
    dru_text = (
        '(version 1)\n'
        '(rule "u2_courtyard_relax"\n'
        '  (condition "A.intersectsCourtyard(\'U2\')")\n'
        '  (constraint track_width (min 0.15mm))\n'
        '  (constraint clearance (min 0.12mm))\n'
        ')\n'
        '(rule "board_min"\n'
        '  (constraint clearance (min 0.15mm))\n'
        ')\n'
    )
    pcb_path.write_text(board_text, encoding="utf-8")
    pro_path.write_text(pro_text, encoding="utf-8")
    dru_path.write_text(dru_text, encoding="utf-8")
    return {
        "pcb_path": pcb_path,
        "pro_path": pro_path,
        "dru_path": dru_path,
        "board_text": board_text,
        "pro_text": pro_text,
        "dru_text": dru_text,
    }


def _install_fake_kipy(cleanup_fn) -> None:
    """Install a minimal fake kipy module tree into sys.modules for exercising KipyLayBoard."""
    saved = {k: sys.modules.get(k) for k in (
        "kipy", "kipy.board_types", "kipy.geometry", "kipy.proto",
        "kipy.proto.board", "kipy.proto.board.board_types_pb2",
    )}

    class _PadStackShape:
        PSS_RECTANGLE = 1
        PSS_ROUNDRECT = 2
        PSS_OVAL = 3
        PSS_CIRCLE = 4

    class _ZoneType:
        ZT_COPPER = 1
        ZT_RULE_AREA = 2

    class _ZoneFillMode:
        ZFM_SOLID = 1

    class _ZoneConnStyle:
        ZCS_THERMAL = 1
        ZCS_FULL = 2
        ZCS_PTH_THERMAL = 3
        ZCS_NONE = 4
        ZCS_INHERITED = 0

    class _IslandRemoval:
        IRM_ALWAYS = 1
        IRM_AREA = 2
        IRM_NEVER = 3

    class _Vector2:
        def __init__(self, x: int = 0, y: int = 0):
            self.x = x
            self.y = y

        @classmethod
        def from_xy(cls, x: int, y: int) -> "_Vector2":
            return cls(x, y)

    class _FakeTrack:
        def __init__(self):
            self.id = ""
            self.layer = 0
            self.width = 0
            self.net = None
            self.start = _Vector2()
            self.end = _Vector2()

    class _FakeVia:
        def __init__(self):
            self.id = ""
            self.position = _Vector2()
            self.diameter = 0
            self.drill_diameter = 0
            self.net = None
            self.padstack_layers = []

    class _FakeZone:
        def __init__(self, proto=None):
            self._proto = proto or types.SimpleNamespace(copper_settings=types.SimpleNamespace(fill_mode=1))
            self.id = ""
            self.type = 1
            self.layers = []
            self.net = None
            self.name = ""

    kipy_mod = types.ModuleType("kipy")
    bt_mod = types.ModuleType("kipy.board_types")
    bt_mod.Track = _FakeTrack
    bt_mod.Via = _FakeVia
    bt_mod.Zone = _FakeZone
    geom_mod = types.ModuleType("kipy.geometry")
    geom_mod.Vector2 = _Vector2
    proto_mod = types.ModuleType("kipy.proto")
    proto_board_mod = types.ModuleType("kipy.proto.board")
    pb_mod = types.ModuleType("kipy.proto.board.board_types_pb2")
    pb_mod.PadStackShape = _PadStackShape
    pb_mod.ZoneType = _ZoneType
    pb_mod.ZoneFillMode = _ZoneFillMode
    pb_mod.ZoneConnectionStyle = _ZoneConnStyle
    pb_mod.IslandRemovalMode = _IslandRemoval

    sys.modules["kipy"] = kipy_mod
    sys.modules["kipy.board_types"] = bt_mod
    sys.modules["kipy.geometry"] = geom_mod
    sys.modules["kipy.proto"] = proto_mod
    sys.modules["kipy.proto.board"] = proto_board_mod
    sys.modules["kipy.proto.board.board_types_pb2"] = pb_mod

    def restore():
        for name, old in saved.items():
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old

    cleanup_fn(restore)


def _make_counting_raw_board(bundle: dict[str, object]):
    """Build a raw IPC-like board stub counting RPC calls, seeded from the dense board bundle."""
    net_objs = [types.SimpleNamespace(code=nid, name=name) for nid, name in DENSE_NETS if name]
    net_by_id = {n.code: n for n in net_objs}

    class _CountingRawBoard:
        def __init__(self):
            self.rpc_counts: dict[str, int] = {
                "get_footprints": 0,
                "get_tracks": 0,
                "get_vias": 0,
                "get_zones": 0,
                "get_nets": 0,
                "get_netclass_for_nets": 0,
                "get_enabled_layers": 0,
                "create_items": 0,
            }
            self.document = types.SimpleNamespace(
                board_filename=str(bundle["pcb_path"].name),
                project=types.SimpleNamespace(path=str(bundle["pro_path"])),
            )
            self._fps = []
            self._tracks = []
            self._vias = []
            self._zones = []
            self._next_id = 0
            self._seed_from_bundle()

        def _seed_from_bundle(self):
            from kicad_sexp import child, children, parse, prop

            root = parse(bundle["board_text"])
            net_id_to_name = {nid: name for nid, name in DENSE_NETS}
            for fp_node in children(root, "footprint"):
                ref = prop(fp_node, "Reference") or ""
                at = child(fp_node, "at") or ["at", "0", "0", "0"]
                fx, fy = float(at[1]), float(at[2])
                frot = float(at[3]) if len(at) > 3 else 0.0
                fp_pads = []
                for pnode in children(fp_node, "pad"):
                    pat = child(pnode, "at") or ["at", "0", "0"]
                    px, py = float(pat[1]), float(pat[2])
                    prot = float(pat[3]) if len(pat) > 3 else 0.0
                    wx, wy = rg.local_to_world(fx, fy, frot, px, py)
                    off = child(pnode, "offset")
                    off_x = float(off[1]) if off and len(off) >= 3 else 0.0
                    off_y = float(off[2]) if off and len(off) >= 3 else 0.0
                    sz = child(pnode, "size") or ["size", "0.6", "0.5"]
                    net_name = rg.resolve_item_net(pnode, net_id_to_name)
                    shape_num = {"rect": 1, "roundrect": 2, "oval": 3, "circle": 4}.get(pnode[3], 1)
                    pad_obj = types.SimpleNamespace(
                        number=pnode[1],
                        net=types.SimpleNamespace(name=net_name),
                        position=types.SimpleNamespace(x=rg.to_nm(wx), y=rg.to_nm(wy)),
                        padstack=types.SimpleNamespace(
                            layers=[0],
                            angle=types.SimpleNamespace(degrees=prot),
                            copper_layers=[types.SimpleNamespace(
                                size=types.SimpleNamespace(x=rg.to_nm(float(sz[1])), y=rg.to_nm(float(sz[2]))),
                                shape=shape_num,
                                offset=types.SimpleNamespace(x=rg.to_nm(off_x), y=rg.to_nm(off_y)),
                                corner_rounding_ratio=0.25 if shape_num == 2 else 0.0,
                            )],
                            drill=None,
                        ),
                    )
                    fp_pads.append(pad_obj)
                self._fps.append(types.SimpleNamespace(
                    reference_field=types.SimpleNamespace(text=types.SimpleNamespace(value=ref)),
                    position=types.SimpleNamespace(x=rg.to_nm(fx), y=rg.to_nm(fy)),
                    orientation=types.SimpleNamespace(as_degrees=lambda r=frot: r),
                    definition=types.SimpleNamespace(pads=fp_pads),
                ))

            layer_ids = {"F.Cu": 0, "In1.Cu": 1, "In2.Cu": 2, "B.Cu": 31}
            for idx, seg in enumerate(children(root, "segment")):
                s, e, w, ly, nt = child(seg, "start"), child(seg, "end"), child(seg, "width"), child(seg, "layer"), child(seg, "net")
                self._tracks.append(types.SimpleNamespace(
                    id=types.SimpleNamespace(value=f"trk-{idx}"),
                    start=types.SimpleNamespace(x=rg.to_nm(float(s[1])), y=rg.to_nm(float(s[2]))),
                    end=types.SimpleNamespace(x=rg.to_nm(float(e[1])), y=rg.to_nm(float(e[2]))),
                    width=rg.to_nm(float(w[1])),
                    layer=layer_ids.get(ly[1], 0),
                    net=net_by_id.get(int(nt[1])),
                ))
            for idx, v in enumerate(children(root, "via")):
                at, sz, dr, nt = child(v, "at"), child(v, "size"), child(v, "drill"), child(v, "net")
                self._vias.append(types.SimpleNamespace(
                    id=types.SimpleNamespace(value=f"via-{idx}"),
                    position=types.SimpleNamespace(x=rg.to_nm(float(at[1])), y=rg.to_nm(float(at[2]))),
                    diameter=rg.to_nm(float(sz[1])),
                    drill_diameter=rg.to_nm(float(dr[1])),
                    net=net_by_id.get(int(nt[1])),
                    padstack_layers=[0, 1, 2, 31],
                ))

        def get_footprints(self):
            self.rpc_counts["get_footprints"] += 1
            return list(self._fps)

        def get_tracks(self):
            self.rpc_counts["get_tracks"] += 1
            return list(self._tracks)

        def get_vias(self):
            self.rpc_counts["get_vias"] += 1
            return list(self._vias)

        def get_zones(self):
            self.rpc_counts["get_zones"] += 1
            return list(self._zones)

        def get_nets(self):
            self.rpc_counts["get_nets"] += 1
            return list(net_objs)

        def get_netclass_for_nets(self, nets):
            self.rpc_counts["get_netclass_for_nets"] += 1
            items = nets if isinstance(nets, (list, tuple)) else [nets]
            out = {}
            for item in items:
                name = getattr(item, "name", str(item))
                nc = "Power" if name in ("GND", "VCC_3V3") else "Default"
                w_mm = 0.5 if nc == "Power" else 0.25
                c_mm = 0.25 if nc == "Power" else 0.2
                out[name] = types.SimpleNamespace(
                    name=nc,
                    clearance=rg.to_nm(c_mm),
                    track_width=rg.to_nm(w_mm),
                    via_diameter=rg.to_nm(0.6),
                    via_drill=rg.to_nm(0.3),
                )
            return out

        def get_enabled_layers(self):
            self.rpc_counts["get_enabled_layers"] += 1
            return [0, 1, 2, 31]

        def begin_commit(self):
            return types.SimpleNamespace(id="commit-1")

        def push_commit(self, commit, description):
            return None

        def drop_commit(self, commit):
            return None

        def create_items(self, items, commit=None):
            self.rpc_counts["create_items"] += 1
            created = []
            for item in items:
                if not getattr(item, "id", ""):
                    self._next_id += 1
                    item.id = types.SimpleNamespace(value=f"new-{self._next_id}")
                if hasattr(item, "start"):
                    self._tracks.append(item)
                else:
                    self._vias.append(item)
                created.append(item)
            return created

        def remove_items(self, items):
            victim_ids = {getattr(getattr(it, "id", None), "value", None) for it in items}
            self._tracks = [t for t in self._tracks if t.id.value not in victim_ids]
            self._vias = [v for v in self._vias if v.id.value not in victim_ids]

        def get_as_string(self):
            return f"{bundle['board_text']}\n; tracks={len(self._tracks)} vias={len(self._vias)}\n"

        def save(self):
            return None

        def get_active_layer(self):
            return 0

    return _CountingRawBoard()


def _make_real_lay_harness(raw_board, bundle: dict[str, object]) -> kicad_lay.KipyLayBoard:
    """Instantiate genuine KipyBoard + KipyLayBoard backed by _CountingRawBoard."""
    layer_id_to_name = {0: "F.Cu", 1: "In1.Cu", 2: "In2.Cu", 31: "B.Cu"}
    layer_name_to_id = {v: k for k, v in layer_id_to_name.items()}

    route = kicad_route.KipyBoard.__new__(kicad_route.KipyBoard)
    route.board = raw_board
    route.canonical_name = lambda lid: layer_id_to_name[lid]
    route.layer_id = lambda name: layer_name_to_id[name]
    route.is_copper = lambda lid: lid in layer_id_to_name
    route.name = "dense-demo"
    route.project = str(bundle["pro_path"])
    route.init_ipc_caches()

    board = kicad_lay.KipyLayBoard.__new__(kicad_lay.KipyLayBoard)
    board.route = route
    board.board = raw_board
    board._init_caches()
    board.run_drc_diff = lambda before_text, after_text: {"new_errors": [], "unconnected": (0, 0)}
    board.quality = lambda: {"errors": {}, "warnings": {}}
    return board


class DenseBoardIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        self.bundle = build_dense_board_bundle(self.root)

    def test_bounded_ipc_rpc_counts_on_20_route_batch_and_unconnected_rollback(self) -> None:
        _install_fake_kipy(self.addCleanup)
        raw = _make_counting_raw_board(self.bundle)
        board = _make_real_lay_harness(raw, self.bundle)

        routes = [
            {
                "from": f"Q{idx + 1}.1",
                "to": f"Q{idx + 1}.2",
                "layer": "F.Cu",
                "width": 0.25,
                "steps": [],
            }
            for idx in range(20)
        ]

        res = kicad_lay.execute_batch(board, {"routes": routes}, run_drc=True)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["status"], "laid")
        self.assertEqual(len(res["routes"]), 20)
        # All 20 routes (40 pad lookups + obstacle checks) hit cached IPC state in O(1) RPCs
        self.assertEqual(raw.rpc_counts["get_footprints"], 1)
        self.assertEqual(raw.rpc_counts["get_nets"], 1)
        self.assertEqual(raw.rpc_counts["get_enabled_layers"], 1)
        self.assertLessEqual(raw.rpc_counts["get_netclass_for_nets"], 1)
        self.assertLessEqual(raw.rpc_counts["get_tracks"], 5)
        self.assertLessEqual(raw.rpc_counts["get_vias"], 5)

        # Verify unconnected-count regression triggers rollback and LayFailure even with 0 DRC errors
        rolled_back = []
        orig_rollback = board.rollback_commit

        def track_rollback(tx, label=""):
            rolled_back.append((tx, label))
            return orig_rollback(tx, label)

        board.rollback_commit = track_rollback
        board.run_drc_diff = lambda before_text, after_text: {"new_errors": [], "unconnected": (1, 2)}
        with self.assertRaises(kicad_lay.LayFailure) as ctx:
            kicad_lay.execute_batch(board, {
                "routes": [{"from": "Q21.1", "to": "Q21.2", "layer": "F.Cu", "width": 0.25, "steps": []}]
            }, run_drc=True)
        self.assertEqual(ctx.exception.status, "drc_failed")
        self.assertEqual(len(rolled_back), 1)

    def test_dense_board_context_preserves_active_box_vias_nets_and_multipads(self) -> None:
        targets = [["U1.1", "U2.1", "SPI_SCK"], ["SW1.1@109.2,102.0", "U1.2", "CLK"]]
        ctx = board_context.extract_board_context(
            self.bundle["pcb_path"],
            targets,
            board_text=self.bundle["board_text"],
            pro_text=self.bundle["pro_text"],
            dru_text=self.bundle["dru_text"],
        )
        sw1_keys = [k for k in ctx["pads"] if k.startswith("SW1.1@")]
        self.assertEqual(len(sw1_keys), 2)
        active_vias = [c for c in ctx["copper"] if c.get("kind") == "via" and 95.0 <= c["at"][0] <= 125.0]
        self.assertEqual(len(active_vias), 6)

        rendered, comp_summary = board_context.render_board_context(
            self.bundle["pcb_path"],
            targets,
            board_text=self.bundle["board_text"],
            pro_text=self.bundle["pro_text"],
            dru_text=self.bundle["dru_text"],
            max_chars=4500,
            max_copper_items=24,
        )
        self.assertLessEqual(len(rendered), 4500)
        self.assertIn("SW1.1@", rendered)
        self.assertIn("SPI_SCK", rendered)
        self.assertIn("CLK", rendered)
        self.assertTrue(any(f"{v['at'][0]:.2f}".rstrip("0").rstrip(".") in rendered for v in active_vias) or "via" in rendered)
        self.assertTrue(comp_summary)

    def test_cross_module_rules_and_geometry_agreement(self) -> None:
        pcb_path = self.bundle["pcb_path"]
        board_text = self.bundle["board_text"]
        pro_rules = rg.load_project_rules(self.bundle["pro_path"], pro_text=self.bundle["pro_text"])
        dru_rules = rg.parse_dru_rules(self.bundle["dru_text"])

        ctx = board_context.extract_board_context(
            pcb_path, [["U1.5", "U2.2", "I2C_SDA"]],
            board_text=board_text, pro_text=self.bundle["pro_text"], dru_text=self.bundle["dru_text"],
        )
        pads_pe = pad_exits._board_pads(board_text)

        # Compare rotated + offset U2.2 pad across board_context, pad_exits, and KipyLayBoard
        u2_ctx = ctx["pads"]["U2.2"]
        u2_pe = pads_pe["U2.2"][0]
        self.assertAlmostEqual(u2_ctx["center"][0], u2_pe["center"][0], places=4)
        self.assertAlmostEqual(u2_ctx["center"][1], u2_pe["center"][1], places=4)
        self.assertEqual(u2_pe["shape"], "oval")

        _install_fake_kipy(self.addCleanup)
        raw = _make_counting_raw_board(self.bundle)
        live_board = _make_real_lay_harness(raw, self.bundle)
        live_u2_2 = live_board.pad("U2.2")
        self.assertAlmostEqual(live_u2_2.copper.center[0], u2_pe["center"][0], places=4)
        self.assertAlmostEqual(live_u2_2.copper.center[1], u2_pe["center"][1], places=4)

        offset_pad = types.SimpleNamespace(
            net=types.SimpleNamespace(name="I2C_SDA"),
            position=types.SimpleNamespace(x=rg.to_nm(116.5), y=rg.to_nm(91.5)),
            padstack=types.SimpleNamespace(
                layers=[0],
                angle=types.SimpleNamespace(degrees=90.0),
                copper_layers=[types.SimpleNamespace(
                    size=types.SimpleNamespace(x=rg.to_nm(1.2), y=rg.to_nm(0.6)),
                    shape=3,
                    offset=types.SimpleNamespace(x=rg.to_nm(0.2), y=rg.to_nm(0.1)),
                    corner_rounding_ratio=0.0,
                )],
            ),
        )
        offset_geom = kicad_ipc.extract_pad_geometry(offset_pad, lambda lid: "F.Cu", lambda lid: True)
        expected_xy = rg.local_to_world(116.5, 91.5, 90.0, 0.2, 0.1)
        self.assertAlmostEqual(offset_geom["center"][0], expected_xy[0], places=4)
        self.assertAlmostEqual(offset_geom["center"][1], expected_xy[1], places=4)

        # Power vs Default netclass clearances match across kicad_rules_geom and board_context
        gnd_nc = rg.resolve_netclass(pro_rules, "GND")
        spi_nc = rg.resolve_netclass(pro_rules, "SPI_SCK")
        self.assertEqual(gnd_nc["name"], "Power")
        self.assertAlmostEqual(gnd_nc["clearance"], 0.25)
        self.assertAlmostEqual(spi_nc["clearance"], 0.20)
        self.assertEqual(len(dru_rules), 2)
        self.assertAlmostEqual(rg.dru_min_clearance_mm(self.bundle["dru_text"]), 0.12)

        # kicad_sexp extracts expected copper counts and zone fills
        parsed_board = kicad_sexp.parse(board_text)
        tracks = kicad_sexp.children(parsed_board, "segment")
        vias = kicad_sexp.children(parsed_board, "via")
        arcs = kicad_sexp.children(parsed_board, "arc")
        self.assertEqual(len(tracks), 340)
        self.assertEqual(len(vias), 44)
        self.assertEqual(len(arcs), 0)
        fills = validate_board.zone_fills(pcb_path)
        self.assertEqual(len(fills), 2)

    def test_editor_step_policy_transcript_and_moved_warning_round_trip(self) -> None:
        # Cleanup-only reduction on 0-open/0-DRC board
        self.assertTrue(editor_step_audit.check_passes(
            {"open": 0, "errors": 0, "findings": 4},
            {"open": 0, "errors": 0, "findings": 1},
            audits_reduced=True,
        ))
        # Policy Call #2: new non-isolated DRC warning fails even when open decreases
        self.assertFalse(editor_step_audit.check_passes(
            {"open": 1, "errors": 0, "findings": 0},
            {"open": 0, "errors": 0, "findings": 1},
            pours_changed=False,
        ))
        # Intermediate pour isolated_copper-only warning is tolerated when pours_changed=True
        self.assertTrue(editor_step_audit.check_passes(
            {"open": 1, "errors": 0, "findings": 0},
            {"open": 0, "errors": 0, "findings": 1, "isolated_copper": 1},
            pours_changed=True,
        ))
        # Non-isolated DRC warning still fails even during pour steps
        self.assertFalse(editor_step_audit.check_passes(
            {"open": 1, "errors": 0, "findings": 0},
            {"open": 0, "errors": 0, "findings": 2, "isolated_copper": 1},
            pours_changed=True,
        ))

        # Truncated agy command logs, valid visual look, then refused no-op edit (must NOT wipe looked_after_edit)
        entries = [
            ("py -3 scripts/kicad_lay.py batch --board board/dense-demo.kicad_pcb...", '{"ok": true, "status": "laid"}', 0, "completed"),
            (
                "py -3 scripts/mac.py look #123 --out /tmp/look.png",
                "800x600 px of window #123 (PCB Editor: dense-demo \u2014 PCB Editor); 1 px = 0.05 mm\n/tmp/look.png",
                0,
                "completed",
            ),
            ("py -3 scripts/kicad_edit.py delete-tracks --id x", '{"ok": false}', 1, "completed"),
        ]
        audited = routing_codex.audit_commands(entries, board_stem="dense-demo")
        self.assertEqual(audited["lay_laid"], 1)
        self.assertTrue(audited["looked_after_edit"])
        self.assertEqual(audited["other"], 0)

        # G3 moved warning round-trip using frozen v0.28 receipt in board/ through both routing_evidence and validate_board
        shutil.copyfile(RECEIPTS_DIR / "run-v028.jsonl", self.root / "board" / "run-v028.jsonl")
        accepted = routing_evidence.accepted_route_quality_warnings(
            self.root, "board/dense-demo.kicad_pcb", [self.root / "board"]
        )
        self.assertEqual(len(accepted), 1)
        exact_item = {
            "severity": "warning", "rule": "via_near_pad", "net": "I2C_SDA",
            "layer": "F.Cu", "point_mm": [118.25, 84.5], "message": "via 0.35mm from U1.8",
        }
        moved_item = dict(exact_item, point_mm=[124.0, 86.0])
        matched, unaccepted, unlisted = routing_evidence.classify_route_quality_warnings(
            {"summary": {"errors": 0, "warnings": 1}, "findings": [exact_item]}, accepted
        )
        self.assertEqual((len(matched), len(unaccepted), unlisted), (1, 0, 0))
        matched2, unaccepted2, unlisted2 = routing_evidence.classify_route_quality_warnings(
            {"summary": {"errors": 0, "warnings": 1}, "findings": [moved_item]}, accepted
        )
        self.assertEqual((len(matched2), len(unaccepted2), unlisted2), (0, 1, 0))

        # Verify validate_board.check_route_quality accepts matching coordinate and fails when moved
        import unittest.mock as mock

        out_dir = self.root / "val_out"
        out_dir.mkdir(parents=True, exist_ok=True)
        fake_desc = types.SimpleNamespace(
            rel=lambda kind: "board/dense-demo.kicad_pcb" if kind == "pcb" else "",
            routing_agent={"tasks": "board/tasks"},
        )
        for item, expected_ok in ((exact_item, True), (moved_item, False)):
            def _fake_offline(script, *cmd_args, _item=item):
                report_path = pathlib.Path(cmd_args[cmd_args.index("--output") + 1])
                report_path.write_text(json.dumps({
                    "summary": {"errors": 0, "warnings": 1, "tracks": 340, "vias": 44},
                    "findings": [_item],
                }), encoding="utf-8")
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")

            with (
                mock.patch.object(validate_board, "ROOT", self.root),
                mock.patch.object(validate_board, "BOARD", self.bundle["pcb_path"]),
                mock.patch.object(validate_board, "DESCRIPTION", fake_desc),
                mock.patch.object(validate_board, "kicad_offline", side_effect=_fake_offline),
            ):
                g3 = validate_board.check_route_quality(out_dir)
                self.assertEqual(g3["ok"], expected_ok, g3)

    def test_frozen_legacy_receipt_fixtures(self) -> None:
        # v0.26: legacy short new_findings + coordinate matching_geometry
        s26, sum26 = routing_report.results((RECEIPTS_DIR / "run-v026.jsonl").read_text(encoding="utf-8"), strict=True)
        self.assertEqual(routing_evidence.adoption_problem(s26, sum26, FIXED_SHA, "codex"), "")
        self.assertIn("1 of 1 steps kept", routing_report.note(s26, sum26, [], "v026demo", provider="codex"))

        # v0.27: partial kept prefix + unkept second step with review_open + candidate.evidence
        s27, sum27 = routing_report.results((RECEIPTS_DIR / "run-v027.jsonl").read_text(encoding="utf-8"), strict=True)
        self.assertEqual(routing_report.outcome(s27, sum27, []), "needs_lead")
        self.assertEqual(routing_evidence.adoption_problem(s27, sum27, s27[0]["reviewed_sha256"], "codex"), "")
        self.assertNotEqual(routing_evidence.adoption_problem(s27, sum27, "0" * 64, "codex"), "")
        note27 = routing_report.note(s27, sum27, [], "v027demo", provider="codex")
        self.assertIn("First failed check:", note27)
        self.assertIn("Saved candidate connects U1.6 to U2.3 on SPI_MOSI.", note27)
        self.assertIn("the review left 1 MUST FIX open", note27)

        # v0.28: obstacle refusal + truncated cmd telemetry + coordinate G3 warning
        s28, sum28 = routing_report.results((RECEIPTS_DIR / "run-v028.jsonl").read_text(encoding="utf-8"), strict=True)
        self.assertEqual(routing_evidence.adoption_problem(s28, sum28, s28[0]["reviewed_sha256"], "codex"), "")
        note28 = routing_report.note(s28, sum28, [], "v028demo", provider="codex")
        self.assertIn("Refusals: obstacle_collision (1x).", note28)

        # v0.281: cleanup-only batch with audits_reduced on 0-open/0-DRC board
        s281, sum281 = routing_report.results((RECEIPTS_DIR / "run-v0281.jsonl").read_text(encoding="utf-8"), strict=True)
        self.assertEqual(routing_evidence.adoption_problem(s281, sum281, s281[0]["reviewed_sha256"], "codex"), "")
        self.assertEqual(routing_report.outcome(s281, sum281, []), "more_work")


if __name__ == "__main__":
    unittest.main()
